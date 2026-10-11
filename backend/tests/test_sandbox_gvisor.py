"""The gVisor backend: the broker against a fake Docker CLI, and the API's client against the
broker in-process."""

import asyncio
import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from aios.config import Settings
from aios.sandbox import broker as broker_module
from aios.sandbox import gvisor as gvisor_module
from aios.sandbox import runner as runner_module
from aios.sandbox.base import SandboxError, SandboxGone, SandboxHandle, SessionSpec
from aios.sandbox.broker import Broker, Completed, OutputTooLarge, create_app, run_command
from aios.sandbox.gvisor import GVisorBackend

TOKEN = "broker-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ENDPOINT = "http://10.40.10.5:8090"
PROXY_URL = "http://sandbox:eyJzIjoiYWdlbnQtcnVuLTQyIn0.c2lnbmF0dXJl@egress-proxy:3128"
SETTINGS = Settings(
    sandbox_broker_token=TOKEN,
    sandbox_broker_advertise_url=ENDPOINT,
    sandbox_proxy_secret="proxy-secret",
    sandbox_image="123.dkr.ecr.us-east-1.amazonaws.com/aios/sandbox:abc",
    sandbox_proxy_image="123.dkr.ecr.us-east-1.amazonaws.com/aios/broker:abc",
)
RESULT = {
    "exit_code": 0,
    "stdout": "4\n",
    "stderr": "",
    "timed_out": False,
    "truncated": False,
    "duration_ms": 12,
}
START = {
    "session_key": "agent-run-42",
    "proxy_url": PROXY_URL,
    "idle_seconds": 900,
    "max_seconds": 7200,
}
# An existing sandbox network as `docker network inspect` describes it: internal, on the subnet
# the hosts' firewall covers.
NETWORK = b"true 172.30.0.0/16"


class FakeDocker:
    """The `docker` CLI: records each call and answers like the real one."""

    def __init__(self, leftovers: str = "", network: bytes | None = None) -> None:
        self.calls: list[dict] = []
        self.leftovers = leftovers
        # `docker network inspect`'s output (whether it's internal, then its subnets), or None
        # if it doesn't exist.
        self.network = network
        self.running: set[str] = set()
        self.removed: list[str] = []
        self.fail_run = False
        self.exec_result: Completed | None = None

    def commands(self, name: str) -> list[list[str]]:
        return [c["argv"] for c in self.calls if c["argv"][1] == name]

    def sandbox_runs(self) -> list[dict]:
        return [c for c in self.calls if c["argv"][1] == "run" and "egress-proxy" not in c["argv"]]

    async def exec(self, container: str, input: bytes, timeout: float, env) -> Completed:
        return self.exec_result or Completed(0, json.dumps(RESULT).encode(), b"")

    async def __call__(self, argv, *, input=None, timeout=60, env=None) -> Completed:
        assert argv[0] == "docker"
        assert all(isinstance(a, str) for a in argv)
        self.calls.append({"argv": argv, "input": input, "timeout": timeout, "env": env})
        command, args = argv[1], argv[2:]
        if command == "ps":
            if "{{.Names}}" in args:
                # Labelled containers only, so never the egress proxy.
                names = sorted(n for n in self.running if n.startswith("aios-sandbox-"))
                return Completed(0, "".join(f"{n}\n" for n in names).encode(), b"")
            return Completed(0, self.leftovers.encode(), b"")
        if command == "rm":
            names = [a for a in args if not a.startswith("--")]
            self.removed += names
            self.running -= set(names)
            return Completed(0, b"", b"")
        if command == "network":
            if args[0] == "inspect":
                if self.network is None:
                    return Completed(1, b"", b"Error: No such network")
                return Completed(0, self.network + b"\n", b"")
            if args[0] == "create":
                [subnet] = [a.removeprefix("--subnet=") for a in args if a.startswith("--subnet=")]
                self.network = f"true {subnet}".encode()
            return Completed(0, b"", b"")
        if command == "run":
            if self.fail_run:
                return Completed(125, b"", b"docker: Error response from daemon: no space left")
            self.running.add(args[args.index("--name") + 1])
            return Completed(0, b"f00d" * 16 + b"\n", b"")
        if command == "inspect":
            running = args[-1] in self.running
            return Completed(0 if running else 1, b"true\n" if running else b"", b"")
        if command == "exec":
            container = args[1]
            if container not in self.running:
                return Completed(1, b"", f"Error: No such container: {container}".encode())
            return await self.exec(container, input, timeout, env)
        raise AssertionError(f"unexpected docker command: {argv}")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_broker(docker=None, settings=SETTINGS, **kwargs) -> Broker:
    return Broker(settings, docker=docker or FakeDocker(), **kwargs)


@pytest.fixture
def docker() -> FakeDocker:
    return FakeDocker(network=NETWORK)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def broker(docker, clock) -> Broker:
    return make_broker(docker, clock=clock)


@pytest.fixture
def client(broker):
    with TestClient(create_app(broker)) as client:
        yield client


def start(client, **changes) -> str:
    resp = client.post("/sessions", json={**START, **changes}, headers=AUTH)
    assert resp.status_code == 201, resp.text
    assert resp.json()["endpoint"] == ENDPOINT
    return resp.json()["id"]


# --- startup -------------------------------------------------------------------------------------


def test_startup_cleans_up_and_sets_up_the_host():
    docker = FakeDocker(leftovers="abc123def4567890\n0123456789ab\n--all\n; rm -rf /\n")
    with TestClient(create_app(make_broker(docker))):
        pass
    assert [c["argv"] for c in docker.calls] == [
        ["docker", "ps", "--all", "--quiet", "--no-trunc", "--filter", "label=aios.sandbox=1"],
        # Only things that look like container ids, never option-like or other output.
        ["docker", "rm", "--force", "--volumes", "abc123def4567890", "0123456789ab"],
        [
            "docker", "network", "inspect", "--format",
            "{{.Internal}}{{range .IPAM.Config}} {{.Subnet}}{{end}}", "aios-sandbox",
        ],
        [
            "docker", "network", "create", "--driver=bridge", "--internal",
            "--subnet=172.30.0.0/16", "--label", "aios.sandbox.network=1", "aios-sandbox",
        ],
        ["docker", "rm", "--force", "egress-proxy"],
        [
            "docker", "run", "--detach", "--name", "egress-proxy", "--restart=unless-stopped",
            "--runtime=runc", "--network=bridge", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--user=1000:1000", "--memory=256m",
            "--pids-limit=256", "--pull=never", "--label", "aios.egress-proxy=1",
            "--env", "SANDBOX_PROXY_SECRET", SETTINGS.sandbox_proxy_image,
            "python", "-m", "aios.sandbox.egress_proxy", "--port", "3128",
        ],
        ["docker", "network", "connect", "--alias", "egress-proxy", "aios-sandbox", "egress-proxy"],
    ]  # fmt: skip
    # The proxy's secret reaches it through the environment, not the command line.
    proxy_run = docker.commands("run")[0]
    assert "proxy-secret" not in " ".join(proxy_run)
    assert docker.calls[5]["env"] == {"SANDBOX_PROXY_SECRET": "proxy-secret"}


def test_startup_keeps_an_internal_network_and_refuses_any_other():
    docker = FakeDocker(network=NETWORK)
    asyncio.run(make_broker(docker).startup())
    assert not [c for c in docker.commands("network") if c[2] == "create"]

    for network in [b"false 172.30.0.0/16", b""]:
        with pytest.raises(RuntimeError, match="isn't --internal"):
            asyncio.run(make_broker(FakeDocker(network=network)).startup())

    # On any other subnet, the hosts' firewall wouldn't keep sandboxes from the host.
    for network in [b"true 10.99.0.0/16", b"true", b"true 172.30.0.0/16 fd00::/64"]:
        docker = FakeDocker(network=network)
        with pytest.raises(RuntimeError, match="SANDBOX_NETWORK_SUBNET"):
            asyncio.run(make_broker(docker).startup())
        assert docker.commands("run") == []


def test_startup_needs_a_token():
    settings = SETTINGS.model_copy(update={"sandbox_broker_token": SecretStr("")})
    with pytest.raises(RuntimeError, match="SANDBOX_BROKER_TOKEN"):
        asyncio.run(make_broker(settings=settings).startup())


def test_startup_skips_the_proxy_without_a_secret():
    docker = FakeDocker(network=NETWORK)
    settings = SETTINGS.model_copy(update={"sandbox_proxy_secret": SecretStr("")})
    asyncio.run(make_broker(docker, settings=settings).startup())
    assert docker.commands("run") == []


def test_startup_advertises_the_instance_ip_from_imdsv2():
    requests = []

    def imds(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/latest/api/token":
            assert request.method == "PUT"
            return httpx.Response(200, text="imds-token")
        assert request.headers["X-aws-ec2-metadata-token"] == "imds-token"
        return httpx.Response(200, text="10.40.11.23")

    settings = SETTINGS.model_copy(update={"sandbox_broker_advertise_url": ""})
    http = httpx.AsyncClient(transport=httpx.MockTransport(imds))
    broker = make_broker(FakeDocker(network=NETWORK), settings=settings, imds=http)
    asyncio.run(broker.startup())
    assert broker.endpoint == "http://10.40.11.23:8090"
    assert [r.url.path for r in requests] == ["/latest/api/token", "/latest/meta-data/local-ipv4"]
    # The client it was given is the caller's to close: a second startup can use it again.
    assert not http.is_closed
    broker.endpoint = ""
    asyncio.run(broker.startup())
    assert broker.endpoint == "http://10.40.11.23:8090"


# --- sessions ------------------------------------------------------------------------------------


def test_start_runs_a_locked_down_gvisor_container(client, docker):
    session_id = start(client)
    [call] = docker.sandbox_runs()
    name = f"aios-sandbox-{session_id}"
    assert call["argv"] == [
        "docker", "run", "--detach", "--name", name,
        "--runtime=runsc",
        "--network=aios-sandbox",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--tmpfs", "/work:rw,nosuid,nodev,size=512m,uid=1001,gid=1001,mode=700",
        "--user=1001:1001",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--memory=1024m", "--memory-swap=1024m", "--cpus=1", "--pids-limit=256",
        "--init", "--log-driver=none", "--pull=never",
        "--label", "aios.sandbox=1", "--label", "aios.session=agent-run-42",
        "--env", "SANDBOX_PROXY_URL",
        SETTINGS.sandbox_image, "sleep", "infinity",
    ]  # fmt: skip
    # The proxy token only travels in the environment.
    assert call["env"] == {"SANDBOX_PROXY_URL": PROXY_URL}
    assert len(session_id) == 32
    assert client.get(f"/sessions/{session_id}", headers=AUTH).status_code == 200


def test_start_without_domains_gets_no_network(client, docker):
    start(client, proxy_url="")
    [run] = [c["argv"] for c in docker.sandbox_runs()]
    assert "--network=none" in run


def test_start_uses_the_configured_runtime_and_sizes(docker):
    settings = SETTINGS.model_copy(
        update={
            "sandbox_docker_runtime": "runc",
            "sandbox_memory_mb": 2048,
            "sandbox_cpus": 0.5,
            "sandbox_pids_limit": 64,
            "sandbox_work_mb": 100,
        }
    )
    with TestClient(create_app(make_broker(docker, settings=settings))) as client:
        start(client)
    [run] = [c["argv"] for c in docker.sandbox_runs()]
    for flag in ["--runtime=runc", "--memory=2048m", "--cpus=0.5", "--pids-limit=64"]:
        assert flag in run
    assert "/work:rw,nosuid,nodev,size=100m,uid=1001,gid=1001,mode=700" in run


def test_requests_need_the_token(client):
    assert client.get("/healthz").json() == {"ok": True}
    for headers in [{}, {"Authorization": "Bearer nope"}, {"Authorization": TOKEN}]:
        resp = client.post("/sessions", json=START, headers=headers)
        assert resp.status_code == 401
        assert client.get(f"/sessions/{'0' * 32}", headers=headers).status_code == 401
        assert client.delete(f"/sessions/{'0' * 32}", headers=headers).status_code == 401


@pytest.mark.parametrize(
    "changes",
    [
        {"session_key": "agent-run-1 --privileged"},
        {"session_key": "--label=x"},
        {"session_key": "x" * 65},
        {"proxy_url": "http://sandbox:tok@egress-proxy:3128\n--privileged"},
        {"proxy_url": "http://sandbox:tok@egress-proxy:3128 --privileged"},
        {"proxy_url": "file:///etc/passwd"},
        {"idle_seconds": 0},
        {"max_seconds": -1},
        {"privileged": True},
    ],
)
def test_start_validates_its_input(client, docker, changes):
    resp = client.post("/sessions", json={**START, **changes}, headers=AUTH)
    assert resp.status_code == 422
    assert docker.sandbox_runs() == []


def test_requests_are_size_capped(client, docker):
    resp = client.post(
        "/sessions",
        content=json.dumps({**START, "pad": "x" * (2 * 1024 * 1024)}),
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert resp.status_code == 413
    session_id = start(client)
    big = {"code": "x" * (2 * 1024 * 1024), "timeout": 5}
    resp = client.post(f"/sessions/{session_id}/exec", json=big, headers=AUTH)
    assert resp.status_code == 413
    assert docker.commands("exec") == []


def test_exec_runs_the_runner_in_the_container(client, docker):
    session_id = start(client)
    resp = client.post(
        f"/sessions/{session_id}/exec", json={"code": "print(2 + 2)", "timeout": 20}, headers=AUTH
    )
    assert (resp.status_code, resp.json()) == (200, RESULT)
    [call] = [c for c in docker.calls if c["argv"][1] == "exec"]
    assert call["argv"] == [
        "docker", "exec", "--interactive", f"aios-sandbox-{session_id}",
        "python", "/opt/sandbox/runner.py", "exec",
    ]  # fmt: skip
    assert json.loads(call["input"]) == {"code": "print(2 + 2)", "timeout": 20}
    assert call["timeout"] == 20 + broker_module.EXEC_GRACE


def test_exec_validates_its_input(client):
    session_id = start(client)
    for body in [{"code": "1", "timeout": 301}, {"code": "1", "timeout": 0}, {"timeout": 5}]:
        resp = client.post(f"/sessions/{session_id}/exec", json=body, headers=AUTH)
        assert resp.status_code == 422


def test_unknown_sessions_are_404(client, docker):
    for session_id in ["0" * 32, "not-a-session", "..%2F..%2Fetc"]:
        assert client.get(f"/sessions/{session_id}", headers=AUTH).status_code == 404
        exec_body = {"code": "1", "timeout": 5}
        resp = client.post(f"/sessions/{session_id}/exec", json=exec_body, headers=AUTH)
        assert resp.status_code == 404
        assert client.delete(f"/sessions/{session_id}", headers=AUTH).status_code == 404
    assert docker.commands("exec") == []
    assert docker.commands("inspect") == []


def test_a_stopped_container_is_gone(client, docker, broker):
    session_id = start(client)
    docker.running.clear()  # e.g. killed for using too much memory
    resp = client.post(f"/sessions/{session_id}/exec", json={"code": "1"}, headers=AUTH)
    assert (resp.status_code, resp.json()["detail"]) == (404, "The sandbox has stopped.")
    assert session_id not in broker.sessions
    assert client.get(f"/sessions/{session_id}", headers=AUTH).status_code == 404

    other = start(client)
    docker.running.clear()
    assert client.get(f"/sessions/{other}", headers=AUTH).status_code == 404
    assert other not in broker.sessions


def test_runner_failures_are_reported(client, docker):
    session_id = start(client)
    url = f"/sessions/{session_id}/exec"
    docker.exec_result = Completed(137, b"", b"killed")
    resp = client.post(url, json={"code": "1"}, headers=AUTH)
    assert (resp.status_code, resp.json()["detail"]) == (502, "The sandbox couldn't run the code.")
    # An answer that isn't the runner's (the code can write to the runner's stdout).
    docker.exec_result = Completed(0, b'{"exit_code": "pwned"}', b"")
    assert client.post(url, json={"code": "1"}, headers=AUTH).status_code == 502

    # The session is still usable after those.
    assert client.get(f"/sessions/{session_id}", headers=AUTH).status_code == 200


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (TimeoutError, 504, "The sandbox didn't finish the call in time, so it was stopped."),
        (OutputTooLarge, 502, "The sandbox's answer was too large, so the sandbox was stopped."),
    ],
)
def test_a_runner_that_may_still_be_going_takes_its_sandbox_with_it(
    client, docker, broker, error, status, detail
):
    # Killing the `docker exec` CLI leaves the runner going in the sandbox: the next call
    # mustn't run beside it, in the same /work.
    session_id = start(client)

    async def killed(*args):
        raise error

    docker.exec = killed
    resp = client.post(f"/sessions/{session_id}/exec", json={"code": "1"}, headers=AUTH)
    assert (resp.status_code, resp.json()["detail"]) == (status, detail)
    assert session_id not in broker.sessions
    assert docker.removed[-1] == f"aios-sandbox-{session_id}"
    resp = client.post(f"/sessions/{session_id}/exec", json={"code": "1"}, headers=AUTH)
    assert resp.status_code == 404


def test_delete_removes_the_container(client, docker, broker):
    session_id = start(client)
    assert client.delete(f"/sessions/{session_id}", headers=AUTH).status_code == 204
    assert docker.removed[-1] == f"aios-sandbox-{session_id}"
    assert broker.sessions == {}
    assert client.delete(f"/sessions/{session_id}", headers=AUTH).status_code == 404


def test_failed_starts_leave_nothing_behind(client, docker, broker):
    docker.fail_run = True
    resp = client.post("/sessions", json=START, headers=AUTH)
    assert (resp.status_code, resp.json()["detail"]) == (502, "Couldn't start a sandbox.")
    assert broker.sessions == {}
    assert docker.removed[-1].startswith("aios-sandbox-")


def test_hosts_have_a_session_limit(docker):
    settings = SETTINGS.model_copy(update={"sandbox_max_sessions": 2})
    with TestClient(create_app(make_broker(docker, settings=settings))) as client:
        start(client)
        last = start(client)
        resp = client.post("/sessions", json=START, headers=AUTH)
        assert resp.status_code == 503
        client.delete(f"/sessions/{last}", headers=AUTH)
        start(client)


def test_reaper_removes_idle_and_expired_sandboxes(client, docker, broker, clock):
    idle = start(client, idle_seconds=60)
    used = start(client, idle_seconds=60)
    old = start(client, idle_seconds=60, max_seconds=100)

    clock.now += 50
    assert asyncio.run(broker.reap()) == []
    client.post(f"/sessions/{used}/exec", json={"code": "1"}, headers=AUTH)
    client.post(f"/sessions/{old}/exec", json={"code": "1"}, headers=AUTH)

    clock.now += 20  # `idle` hasn't been used for 70s; the others for 20s.
    assert asyncio.run(broker.reap()) == [idle]
    clock.now += 40  # `old` is 110s old; `used` was last used 60s ago.
    assert asyncio.run(broker.reap()) == [old]
    assert set(broker.sessions) == {used}
    assert {f"aios-sandbox-{idle}", f"aios-sandbox-{old}"} <= set(docker.removed)


def test_reaper_removes_containers_without_a_session(client, docker, broker):
    kept = start(client)
    failed_rm = start(client)

    async def rm_times_out(argv, **kwargs):
        if argv[1] == "rm":
            raise TimeoutError
        return await FakeDocker.__call__(docker, argv, **kwargs)

    # `docker rm` times out: the session is dropped but its container is still there.
    broker._run = rm_times_out
    assert client.delete(f"/sessions/{failed_rm}", headers=AUTH).status_code == 204
    # A timed-out `docker run` whose container the daemon created anyway, after its rm.
    late = "aios-sandbox-" + "e" * 32
    docker.running |= {late, "aios-sandbox-not-ours", "egress-proxy"}
    broker._run = docker

    assert asyncio.run(broker.reap()) == []
    [ps] = [c for c in docker.commands("ps") if "{{.Names}}" in c]
    assert ps == [
        "docker", "ps", "--all", "--filter", "label=aios.sandbox=1", "--format", "{{.Names}}",
    ]  # fmt: skip
    rm = docker.commands("rm")[-1]
    assert rm[:4] == ["docker", "rm", "--force", "--volumes"]
    assert sorted(rm[4:]) == sorted([f"aios-sandbox-{failed_rm}", late])
    assert set(broker.sessions) == {kept}
    assert f"aios-sandbox-{kept}" in docker.running


def test_busy_sandboxes_are_never_idle(broker, clock):
    session = broker_module.Session("a" * 32, "c", "k", 60, 600, clock.now, clock.now, busy=1)
    broker.sessions[session.id] = session
    clock.now += 120
    assert asyncio.run(broker.reap()) == []
    clock.now += 600
    assert asyncio.run(broker.reap()) == [session.id]


# --- run_command ---------------------------------------------------------------------------------


def test_run_command_feeds_stdin_and_captures_output():
    code = "import os, sys; print(sys.stdin.read().upper(), os.environ['EXTRA'])"
    done = asyncio.run(
        run_command([sys.executable, "-c", code], input=b"hello", env={"EXTRA": "x"})
    )
    assert done == Completed(0, b"HELLO x\n", b"")


def test_run_command_kills_slow_or_chatty_commands():
    with pytest.raises(TimeoutError):
        asyncio.run(run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1))
    chatty = "import sys; sys.stdout.write('x' * 5_000_000)"
    with pytest.raises(OutputTooLarge):
        asyncio.run(run_command([sys.executable, "-c", chatty]))


# --- the API's client ----------------------------------------------------------------------------

SPEC = SessionSpec(
    session_key="agent-run-42", proxy_url=PROXY_URL, idle_seconds=900, max_seconds=7200
)


def in_process(broker: Broker) -> GVisorBackend:
    """A client whose requests (to any host) go to `broker`'s app, in process."""
    settings = SETTINGS.model_copy(update={"sandbox_broker_url": "http://brokers.internal:8090/"})
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(broker)))
    return GVisorBackend(settings, http=http)


def test_client_runs_sessions_through_the_broker(broker):
    async def scenario():
        await broker.startup()
        backend = in_process(broker)
        handle = await backend.start(SPEC)
        assert (handle.endpoint, handle.secret) == (ENDPOINT, "")
        assert handle.ref in broker.sessions
        assert await backend.alive(handle)
        result = await backend.execute(handle, "print(2 + 2)", 10)
        assert result.stdout == "4\n"
        await backend.stop(handle)
        assert not await backend.alive(handle)
        with pytest.raises(SandboxGone):
            await backend.execute(handle, "print(1)", 10)
        # Stopping twice is fine.
        await backend.stop(handle)

    asyncio.run(scenario())


def test_client_reports_broker_errors(broker, docker):
    async def scenario():
        await broker.startup()
        backend = in_process(broker)
        docker.fail_run = True
        with pytest.raises(SandboxError, match="Couldn't start a sandbox"):
            await backend.start(SPEC)
        docker.fail_run = False
        handle = await backend.start(SPEC)
        docker.exec_result = Completed(1, b"", b"boom")
        with pytest.raises(SandboxError, match="couldn't run the code") as error:
            await backend.execute(handle, "1", 10)
        assert not isinstance(error.value, SandboxGone)

        wrong = GVisorBackend(SETTINGS, http=backend.http)
        wrong.url, wrong.token = backend.url, "nope"
        with pytest.raises(SandboxError, match="refused this server's credentials"):
            await wrong.start(SPEC)

    asyncio.run(scenario())


def test_client_treats_an_unreachable_host_as_gone():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    settings = SETTINGS.model_copy(update={"sandbox_broker_url": "http://brokers.internal:8090"})
    backend = GVisorBackend(settings, http=httpx.AsyncClient(transport=httpx.MockTransport(down)))
    handle = SandboxHandle("a" * 32, ENDPOINT, "")

    async def scenario():
        with pytest.raises(SandboxGone):
            await backend.execute(handle, "1", 10)
        # `alive()` can't tell, so it doesn't say the sandbox has gone: the next call finds out.
        with pytest.raises(SandboxError, match="Couldn't reach the sandbox") as error:
            await backend.alive(handle)
        assert not isinstance(error.value, SandboxGone)
        await backend.stop(handle)
        with pytest.raises(SandboxError, match="Couldn't reach"):
            await backend.start(SPEC)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(503, json={"detail": "busy"}),
        httpx.Response(500, text="oops"),
        httpx.Response(401),
        httpx.ReadTimeout("slow"),
    ],
)
def test_client_alive_only_reports_gone_when_the_broker_says_so(answer):
    def broker(request: httpx.Request) -> httpx.Response:
        if isinstance(answer, Exception):
            raise answer
        return answer

    backend = GVisorBackend(SETTINGS, http=httpx.AsyncClient(transport=httpx.MockTransport(broker)))
    handle = SandboxHandle("a" * 32, ENDPOINT, "")
    with pytest.raises(SandboxError) as error:
        asyncio.run(backend.alive(handle))
    assert not isinstance(error.value, SandboxGone)

    gone = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert asyncio.run(GVisorBackend(SETTINGS, http=gone).alive(handle)) is False


def test_client_timeouts_and_odd_answers():
    seen = []

    def broker(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/sessions":
            return httpx.Response(201, json={"id": "a" * 32, "endpoint": "file:///etc"})
        raise httpx.ReadTimeout("slow", request=request)

    settings = SETTINGS.model_copy(update={"sandbox_broker_url": "http://brokers.internal:8090"})
    backend = GVisorBackend(settings, http=httpx.AsyncClient(transport=httpx.MockTransport(broker)))

    async def scenario():
        with pytest.raises(SandboxError, match="invalid address"):
            await backend.start(SPEC)
        with pytest.raises(SandboxError, match="didn't respond") as error:
            await backend.execute(SandboxHandle("a" * 32, ENDPOINT, ""), "1", 10)
        assert not isinstance(error.value, SandboxGone)
        timeout = seen[-1].extensions["timeout"]
        assert timeout["read"] == 10 + 30
        assert seen[-1].headers["Authorization"] == f"Bearer {TOKEN}"

    asyncio.run(scenario())


def test_client_tries_another_host_when_one_is_full(monkeypatch):
    monkeypatch.setattr(gvisor_module, "START_RETRY_DELAY", 0)
    answers: list[httpx.Response] = []
    seen: list[httpx.Request] = []

    def brokers(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answers.pop(0)

    settings = SETTINGS.model_copy(update={"sandbox_broker_url": "http://brokers.internal:8090"})
    backend = GVisorBackend(
        settings, http=httpx.AsyncClient(transport=httpx.MockTransport(brokers))
    )
    full = httpx.Response(503, json={"detail": "No sandbox capacity right now; try again shortly."})
    started = httpx.Response(201, json={"id": "a" * 32, "endpoint": ENDPOINT})

    answers[:] = [full, full, started]
    assert asyncio.run(backend.start(SPEC)).ref == "a" * 32
    # Each on a new connection, which the load balancer may send to another host.
    assert [r.headers["Connection"] for r in seen] == ["close"] * 3

    seen.clear()
    answers[:] = [full] * gvisor_module.START_ATTEMPTS
    with pytest.raises(SandboxError, match="No sandbox capacity"):
        asyncio.run(backend.start(SPEC))
    assert len(seen) == gvisor_module.START_ATTEMPTS

    # Other failures aren't about one host's capacity.
    seen.clear()
    answers[:] = [httpx.Response(502, json={"detail": "Couldn't start a sandbox."})]
    with pytest.raises(SandboxError, match="Couldn't start a sandbox"):
        asyncio.run(backend.start(SPEC))
    assert len(seen) == 1


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(201, text="not json"),
        httpx.Response(201, json={"id": "a" * 32}),
        httpx.Response(201, json={"endpoint": ENDPOINT}),
        httpx.Response(201, json=["a" * 32, ENDPOINT]),
        httpx.Response(201, json=None),
    ],
)
def test_client_reports_a_malformed_start_answer(answer):
    backend = GVisorBackend(
        SETTINGS.model_copy(update={"sandbox_broker_url": "http://brokers.internal:8090"}),
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: answer)),
    )
    with pytest.raises(SandboxError, match="invalid answer"):
        asyncio.run(backend.start(SPEC))


@pytest.mark.parametrize("answer", [{"text": "not json"}, {"json": {"exit_code": "pwned"}}])
def test_client_reports_a_malformed_exec_answer(answer):
    backend = GVisorBackend(
        SETTINGS,
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, **answer))
        ),
    )
    with pytest.raises(SandboxError, match="invalid answer") as error:
        asyncio.run(backend.execute(SandboxHandle("a" * 32, ENDPOINT, ""), "1", 10))
    assert not isinstance(error.value, SandboxGone)


def test_client_refuses_a_proxy_token_the_broker_would(broker):
    def proxy_url(length: int) -> str:
        return f"http://sandbox:{'t' * length}@egress-proxy:3128"

    async def scenario():
        await broker.startup()
        backend = in_process(broker)
        longest = replace(SPEC, proxy_url=proxy_url(gvisor_module.PROXY_TOKEN_LIMIT))
        assert (await backend.start(longest)).ref in broker.sessions
        sessions = len(broker.sessions)
        too_long = replace(SPEC, proxy_url=proxy_url(gvisor_module.PROXY_TOKEN_LIMIT + 1))
        with pytest.raises(SandboxError, match="list of reachable domains is too long"):
            await backend.start(too_long)
        assert len(broker.sessions) == sessions

    asyncio.run(scenario())


def test_client_needs_a_broker_url():
    with pytest.raises(SandboxError, match="isn't set up"):
        asyncio.run(GVisorBackend(SETTINGS).start(SPEC))


# --- end to end ----------------------------------------------------------------------------------


class RunnerDocker(FakeDocker):
    """`docker exec` runs the real runner (`runner.py exec`), with a working directory per
    container that persists between calls, like the container's tmpfs /work."""

    def __init__(self, root: Path) -> None:
        super().__init__(network=NETWORK)
        self.root = root
        self.env: dict[str, dict[str, str]] = {}

    async def __call__(self, argv, *, input=None, timeout=60, env=None) -> Completed:
        if argv[1] == "run":
            self.env[argv[argv.index("--name") + 1]] = env or {}
        return await super().__call__(argv, input=input, timeout=timeout, env=env)

    async def exec(self, container, input, timeout, env) -> Completed:
        workdir = self.root / container
        return await run_command(
            [sys.executable, runner_module.__file__, "exec"],
            input=input,
            timeout=timeout,
            env={**self.env[container], "SANDBOX_WORKDIR": str(workdir)},
        )


@pytest.fixture
def runner_docker():
    # Not under pytest's tmp_path, whose parents only its owner can enter: run as root, the
    # runner runs the code as another user, which needs to reach the working directory.
    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o755)
        yield RunnerDocker(Path(root))


def test_end_to_end_files_persist_between_calls(runner_docker):
    broker = make_broker(runner_docker)

    async def scenario():
        await broker.startup()
        backend = in_process(broker)
        handle = await backend.start(SPEC)
        first = await backend.execute(handle, "open('notes.txt', 'w').write('kept')", 10)
        assert first.exit_code == 0, first.stderr
        second = await backend.execute(
            handle, "import os\nprint(open('notes.txt').read(), os.environ['HTTPS_PROXY'])", 10
        )
        assert second.stdout == f"kept {PROXY_URL}\n"
        slow = await backend.execute(handle, "import time\ntime.sleep(10)", 1)
        assert slow.timed_out

        # Another run's sandbox has a /work of its own.
        other = await backend.start(SPEC)
        result = await backend.execute(other, "import os\nprint(os.path.exists('notes.txt'))", 10)
        assert result.stdout == "False\n"

    asyncio.run(scenario())
