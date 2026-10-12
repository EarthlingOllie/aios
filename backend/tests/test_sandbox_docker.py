"""The local Docker backend (`aios.sandbox.docker`).

The tests that start containers need a Docker daemon and the sandbox image (`docker build
--target sandbox -t aios-sandbox:local backend`), and skip without them.
"""

import asyncio
import json
import shutil
import subprocess
import sys

import pytest

from aios.config import Settings
from aios.sandbox.base import SandboxError, SandboxGone, SandboxHandle, SessionSpec
from aios.sandbox.docker import MAX_ANSWER, DockerBackend

IMAGE = "aios-sandbox:local"


def backend(**settings) -> DockerBackend:
    return DockerBackend(Settings(sandbox_docker_image=IMAGE, **settings))


def spec(proxy_url: str = "", idle: int = 600, total: int = 3600) -> SessionSpec:
    return SessionSpec("agent-run-1", proxy_url, idle_seconds=idle, max_seconds=total)


def test_run_args_isolate_the_sandbox():
    args = backend().run_args("aios-sandbox-x", spec())
    assert "--network=none" in args
    for flag in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--rm"):
        assert flag in args
    assert "--user=1001:1001" in args
    assert args[args.index("--label") + 1] == "aios.sandbox=1"


def test_run_args_give_the_code_its_memory_on_top_of_the_tmpfs_files():
    args = backend(sandbox_docker_memory_mb=1024).run_args("aios-sandbox-x", spec())
    # 1024 MB for the code, plus 512 MB of /work and 64 MB of /tmp.
    assert "--memory=1600m" in args
    assert "--memory-swap=1600m" in args
    assert any(a.startswith("/work:") and "size=512m" in a for a in args)
    assert any(a.startswith("/tmp:") and "size=64m" in a for a in args)


def test_run_args_keep_the_proxy_token_out_of_argv():
    url = "http://sandbox:secret-token@host.docker.internal:3128"
    args = backend().run_args("aios-sandbox-x", spec(proxy_url=url))
    assert "--network=bridge" in args
    assert not any("secret-token" in a for a in args)
    assert args[args.index("--env") + 1] == "SANDBOX_PROXY_URL"


def test_missing_docker_is_a_sandbox_error():
    b = DockerBackend(Settings(), docker="/nonexistent/docker")
    with pytest.raises(SandboxError, match="Docker isn't available"):
        asyncio.run(b.start(spec()))
    assert asyncio.run(b.alive(SandboxHandle("x", "", ""))) is False


def test_a_bad_answer_is_logged_with_its_cause(monkeypatch, caplog):
    b = backend()

    async def run(*args, **kwargs):
        return 0, b"not json", b""

    async def alive(handle):
        return True

    monkeypatch.setattr(b, "_run", run)
    monkeypatch.setattr(b, "alive", alive)
    with pytest.raises(SandboxError, match="couldn't run the code"):
        asyncio.run(b.execute(SandboxHandle("aios-sandbox-x", "", ""), "print(1)", 30))
    assert "runner failed in aios-sandbox-x (exit 0, 8 bytes of output): " in caplog.text
    assert "b''" not in caplog.text


def test_a_cancelled_call_removes_the_sandbox(monkeypatch):
    # Cancelling only kills the `docker exec` CLI, so the runner would go on in the sandbox.
    b = backend()
    calls = []

    async def fake_run(*args, **kwargs):
        calls.append(args[0])
        if args[0] == "exec":
            await asyncio.Event().wait()
        return 0, b"", b""

    monkeypatch.setattr(b, "_run", fake_run)

    async def scenario():
        call = asyncio.create_task(b.execute(SandboxHandle("x", "", ""), "print(1)", 30))
        await asyncio.sleep(0)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(scenario())
    assert calls == ["exec", "rm"]


# Stands in for the CLI: echoes stdin then floods stdout without end, as code in the sandbox
# can through the runner's stdout. `inspect` says the container is running.
FLOODING_DOCKER = """
import sys
if sys.argv[1] == "inspect":
    print("true")
    sys.exit()
sys.stdout.buffer.write(sys.stdin.buffer.read())
while True:
    sys.stdout.buffer.write(b"x" * 65536)
"""


def test_output_past_the_limit_is_cut_off(tmp_path):
    fake = tmp_path / "docker"
    fake.write_text(f"#!{sys.executable}\n{FLOODING_DOCKER}")
    fake.chmod(0o755)
    b = DockerBackend(Settings(), docker=str(fake))
    code, stdout, _ = asyncio.run(b._run("exec", input=b"payload", timeout=30))
    assert code != 0
    assert len(stdout) == MAX_ANSWER + 1
    assert stdout.startswith(b"payloadx")
    with pytest.raises(SandboxError, match="couldn't run the code"):
        asyncio.run(b.execute(SandboxHandle("x", "", ""), "print(1)", 30))


def test_docker_cli_gets_only_what_it_needs(tmp_path, monkeypatch):
    # A stand-in `docker` that logs its command and environment, and fails.
    log = tmp_path / "calls.jsonl"
    fake = tmp_path / "docker"
    fake.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"with open({str(log)!r}, 'a') as f:\n"
        "    f.write(json.dumps([sys.argv[1], dict(os.environ)]) + '\\n')\n"
        "sys.exit(1)\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("SANDBOX_PROXY_SECRET", "api-secret")
    monkeypatch.setenv("DOCKER_HOST", "unix:///run/docker.sock")
    b = DockerBackend(Settings(), docker=str(fake))
    with pytest.raises(SandboxError, match="Couldn't start"):
        asyncio.run(b.start(spec(proxy_url="http://sandbox:token@host:3128")))
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    # `docker run`, then the cleanup's `docker rm`.
    assert [command for command, _ in calls] == ["run", "rm"]
    for _, env in calls:
        assert "SANDBOX_PROXY_SECRET" not in env
        assert env["DOCKER_HOST"] == "unix:///run/docker.sock"
    assert calls[0][1]["SANDBOX_PROXY_URL"] == "http://sandbox:token@host:3128"
    assert "SANDBOX_PROXY_URL" not in calls[1][1]


# --- against a real Docker daemon ----------------------------------------------------------------


def _image_available() -> bool:
    if not shutil.which("docker"):
        return False
    done = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
    return done.returncode == 0


docker_only = pytest.mark.skipif(not _image_available(), reason=f"needs Docker and {IMAGE}")


@docker_only
def test_runs_code_and_keeps_work_between_calls():
    b = backend()

    async def scenario():
        handle = await b.start(spec())
        try:
            assert await b.alive(handle)
            first = await b.execute(handle, "open('a.txt', 'w').write('hi'); print(6 * 7)", 30)
            assert (first.exit_code, first.stdout) == (0, "42\n")
            second = await b.execute(
                handle, "import os; print(open('a.txt').read(), os.getuid())", 30
            )
            assert second.stdout == "hi 1001\n"
            # No network without allowed domains.
            offline = await b.execute(
                handle, "import socket; socket.create_connection(('1.1.1.1', 443), 3)", 30
            )
            assert offline.exit_code != 0
            # The root filesystem is read-only.
            readonly = await b.execute(handle, "open('/opt/x', 'w')", 30)
            assert "Read-only file system" in readonly.stderr
            timed_out = await b.execute(handle, "import time; time.sleep(10)", 1)
            assert timed_out.timed_out and timed_out.exit_code is None
        finally:
            await b.stop(handle)
        assert not await b.alive(handle)
        with pytest.raises(SandboxGone):
            await b.execute(handle, "print(1)", 30)

    asyncio.run(scenario())


@docker_only
def test_stops_itself_when_idle():
    b = backend()

    async def scenario():
        handle = await b.start(spec(idle=1))
        try:
            for _ in range(30):
                if not await b.alive(handle):
                    return
                await asyncio.sleep(0.5)
            pytest.fail("the sandbox didn't stop when idle")
        finally:
            await b.stop(handle)

    asyncio.run(scenario())


@docker_only
def test_a_busy_sandbox_is_not_idle():
    b = backend()

    async def scenario():
        handle = await b.start(spec(idle=1))
        try:
            result = await b.execute(handle, "import time; time.sleep(5); print('done')", 30)
            assert result.stdout == "done\n"
        finally:
            await b.stop(handle)

    asyncio.run(scenario())


@docker_only
def test_missing_image_is_reported():
    b = DockerBackend(Settings(sandbox_docker_image="aios-sandbox:does-not-exist"))
    with pytest.raises(SandboxError, match="isn't built"):
        asyncio.run(b.start(spec()))
