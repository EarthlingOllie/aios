"""The local Docker backend (`aios.sandbox.docker`).

The tests that start containers need a Docker daemon and the sandbox image (`docker build
--target sandbox -t aios-sandbox:local backend`), and skip without them.
"""

import asyncio
import shutil
import subprocess

import pytest

from aios.config import Settings
from aios.sandbox.base import SandboxError, SandboxGone, SandboxHandle, SessionSpec
from aios.sandbox.docker import DockerBackend

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
