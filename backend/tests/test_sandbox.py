"""The parts of `aios.sandbox` every backend shares: tokens, the egress proxy and the runner."""

import asyncio
import base64
import json
import os
import socket
import threading

import httpx
import pytest

from aios.sandbox import egress_proxy, proxy_token, runner

SECRET = "test-secret"


# --- proxy tokens --------------------------------------------------------------------------------


def test_proxy_tokens_carry_their_grant():
    token = proxy_token.mint(SECRET, session="agent-run-1", domains=["a.com"], ttl=60, now=1000)
    grant = proxy_token.verify(SECRET, token, now=1059)
    assert grant == proxy_token.ProxyGrant("agent-run-1", ("a.com",), 1060)
    # Expired, signed with another secret, tampered with, or not a token at all.
    assert proxy_token.verify(SECRET, token, now=1061) is None
    assert proxy_token.verify("other", token, now=1000) is None
    payload, signature = token.split(".")
    forged = base64.urlsafe_b64encode(
        json.dumps({"s": "agent-run-1", "d": ["*.com"], "e": 2000}).encode()
    )
    assert proxy_token.verify(SECRET, f"{forged.decode()}.{signature}", now=1000) is None
    assert proxy_token.verify(SECRET, "garbage", now=1000) is None
    assert proxy_token.verify(SECRET, "abc.\u00e9", now=1000) is None
    assert proxy_token.verify(SECRET, f"{payload}\u00e9.{signature}", now=1000) is None
    assert proxy_token.verify("", token, now=1000) is None


def test_proxy_allowlist_matching():
    domains = ["api.example.com", "*.data.gov"]
    assert proxy_token.allows(domains, "api.example.com")
    assert proxy_token.allows(domains, "API.Example.com.")
    assert not proxy_token.allows(domains, "example.com")
    assert not proxy_token.allows(domains, "evil-api.example.com")
    assert proxy_token.allows(domains, "catalog.data.gov")
    assert proxy_token.allows(domains, "a.b.data.gov")
    assert not proxy_token.allows(domains, "data.gov")
    assert not proxy_token.allows(domains, "baddata.gov")


# --- egress proxy --------------------------------------------------------------------------------


def proxy_exchange(request: bytes, *, addresses=("93.184.215.14",), **options) -> tuple[str, bytes]:
    """Sends `request` to a proxy whose upstream connections all go to a local echo server.

    Returns the proxy's response head, and what came back through the tunnel after sending
    `ping`.
    """

    async def scenario():
        async def echo(reader, writer):
            writer.write(b"echo:" + await reader.read(100))
            await writer.drain()
            writer.close()

        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        upstream_port = upstream.sockets[0].getsockname()[1]
        connected = []

        async def resolver(host, port):
            return list(addresses)

        async def connect(address, port):
            connected.append((address, port))
            return await asyncio.open_connection("127.0.0.1", upstream_port)

        proxy = await asyncio.start_server(
            lambda r, w: egress_proxy.handle(
                r, w, secret=SECRET, resolver=resolver, connect=connect, **options
            ),
            "127.0.0.1",
            0,
        )
        reader, writer = await asyncio.open_connection(*proxy.sockets[0].getsockname()[:2])
        writer.write(request)
        await writer.drain()
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        tunnel = b""
        if head.startswith("HTTP/1.1 200"):
            writer.write(b"ping")
            await writer.drain()
            tunnel = await reader.read(100)
            assert connected == [(addresses[0], 443)]
        writer.close()
        proxy.close()
        upstream.close()
        return head, tunnel

    return asyncio.run(scenario())


def connect_request(host: str, token: str | None, port: int = 443, method="CONNECT") -> bytes:
    auth = ""
    if token is not None:
        credentials = base64.b64encode(f"sandbox:{token}".encode()).decode()
        auth = f"Proxy-Authorization: Basic {credentials}\r\n"
    return f"{method} {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n{auth}\r\n".encode()


def token(domains=("api.example.com",), ttl=60) -> str:
    return proxy_token.mint(SECRET, session="agent-run-7", domains=list(domains), ttl=ttl)


def test_egress_proxy_tunnels_to_allowed_hosts():
    head, tunnel = proxy_exchange(connect_request("api.example.com", token()))
    assert head.startswith("HTTP/1.1 200 Connection Established")
    assert tunnel == b"echo:ping"


@pytest.mark.parametrize(
    ("request_bytes", "status", "reason"),
    [
        (connect_request("api.example.com", None), "407", "missing or invalid proxy token"),
        (connect_request("api.example.com", "forged.token"), "407", "invalid proxy token"),
        (connect_request("api.example.com", "abc.\u00e9"), "407", "invalid proxy token"),
        (connect_request("api.example.com", token(ttl=-1)), "407", "invalid proxy token"),
        (connect_request("other.com", token()), "403", "isn't in this sandbox's allowed domains"),
        (connect_request("api.example.com", token(), port=80), "403", "only port 443"),
        (connect_request("api.example.com", token(), method="GET"), "405", "only HTTPS"),
        (b"\r\n\r\n", "405", "only HTTPS"),
    ],
)
def test_egress_proxy_refusals(request_bytes, status, reason):
    head, _ = proxy_exchange(request_bytes)
    assert head.startswith(f"HTTP/1.1 {status}")


def test_egress_proxy_refuses_private_addresses():
    # An allowed name that resolves into the VPC, to the metadata endpoint, or to loopback.
    for address in ["10.40.1.5", "169.254.169.254", "127.0.0.1", "::1", "fd00::1"]:
        head, _ = proxy_exchange(connect_request("api.example.com", token()), addresses=[address])
        assert head.startswith("HTTP/1.1 403"), address
    # A public address alongside a private one: only the public one is used.
    head, tunnel = proxy_exchange(
        connect_request("api.example.com", token()), addresses=["93.184.215.14", "10.0.0.1"]
    )
    assert tunnel == b"echo:ping"


# --- runner --------------------------------------------------------------------------------------


def test_runner_runs_code_in_the_workdir(workdir):
    first = runner.execute("open('notes.txt', 'w').write('kept')\nprint('hello')", 10)
    assert (first["exit_code"], first["stdout"], first["timed_out"]) == (0, "hello\n", False)
    # A new process each call, but files in the working directory carry over.
    second = runner.execute("print(open('notes.txt').read())", 10)
    assert second["stdout"] == "kept\n"
    assert (workdir / "notes.txt").read_text() == "kept"


def test_runner_reports_failures_and_timeouts(workdir):
    failed = runner.execute("import sys\nprint('to stderr', file=sys.stderr)\nsys.exit(3)", 10)
    assert (failed["exit_code"], failed["stderr"]) == (3, "to stderr\n")
    error = runner.execute("1 / 0", 10)
    assert error["exit_code"] == 1
    assert "ZeroDivisionError" in error["stderr"]
    slow = runner.execute("import time\nprint('started', flush=True)\ntime.sleep(30)", 1)
    assert (slow["exit_code"], slow["timed_out"], slow["stdout"]) == (None, True, "started\n")
    assert slow["duration_ms"] < 10_000


def test_runner_caps_output(workdir, monkeypatch):
    monkeypatch.setattr(runner, "OUTPUT_LIMIT", 100)
    result = runner.execute("print('x' * 10_000_000)", 30)
    assert (result["exit_code"], len(result["stdout"]), result["truncated"]) == (0, 100, True)


def test_runner_stops_background_processes(workdir):
    code = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])"
    )
    result = runner.execute(code, 10)
    # The call returns as soon as the code exits, rather than waiting on the background process
    # (which holds the output pipes), because the whole process group is killed.
    assert result["exit_code"] == 0
    assert result["duration_ms"] < 10_000


@pytest.mark.parametrize(
    "start",
    [
        # Its own session, so outside the call's process group, still holding stdout.
        "subprocess.Popen(sleeper, start_new_session=True)",
        # Daemonised: its parent exits, so it's orphaned as well as in a new session.
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    subprocess.Popen(sleeper)\n"
        "    os._exit(0)\n"
        "os.waitpid(pid, 0)",
    ],
)
def test_runner_stops_processes_that_leave_the_group(workdir, start):
    code = (
        "import os, subprocess, sys, time\n"
        "sleeper = [sys.executable, '-c',"
        ' \'import os, time; open("pid", "w").write(str(os.getpid())); time.sleep(60)\']\n'
        f"{start}\n"
        "while not os.path.exists('pid'): time.sleep(0.01)\n"
    )
    result = runner.execute(code, 10)
    assert result["exit_code"] == 0, result["stderr"]
    # Not held up waiting for the background process to close the output pipes.
    assert result["duration_ms"] < 4_000
    # Gone, and reaped, rather than left running as the sandbox user.
    pid = int((workdir / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_runner_gives_the_code_a_minimal_environment(workdir, monkeypatch):
    monkeypatch.setenv("SANDBOX_TOKEN", "runner-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("SANDBOX_PROXY_URL", "http://sandbox:t@proxy:3128")
    result = runner.execute("import os, json\nprint(json.dumps(dict(os.environ)))", 10)
    env = json.loads(result["stdout"])
    assert "SANDBOX_TOKEN" not in env
    assert "DATABASE_URL" not in env
    assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://sandbox:t@proxy:3128"
    assert env["HOME"] == str(workdir)
    limits = runner.execute("import resource\nprint(resource.getrlimit(resource.RLIMIT_CORE))", 10)
    assert limits["stdout"] == "(0, 0)\n"


def test_runner_exec_mode(workdir, monkeypatch):
    import io
    import sys

    monkeypatch.setattr(sys, "argv", ["runner.py", "exec"])
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"code": "print(6 * 7)", "timeout": 5}))
    )
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    runner.main()
    assert json.loads(out.getvalue())["stdout"] == "42\n"


def test_runner_serve_mode(workdir):
    server = runner.Server(("127.0.0.1", 0), "tok", idle_seconds=60, max_seconds=600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert httpx.get(f"{url}/health").json() == {"ok": True}
        body = {"code": "print('served')", "timeout": 5}
        assert httpx.post(f"{url}/exec", json=body).status_code == 401
        wrong = httpx.post(f"{url}/exec", json=body, headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401
        auth = {"Authorization": "Bearer tok"}
        resp = httpx.post(f"{url}/exec", json=body, headers=auth)
        assert (resp.status_code, resp.json()["stdout"]) == (200, "served\n")
        assert httpx.post(f"{url}/exec", json={"nope": 1}, headers=auth).status_code == 400
        # A bad length is refused, rather than dropping the connection or reading until the
        # client closes it.
        for length in ["-1", "abc"]:
            with socket.create_connection(server.server_address[:2], timeout=5) as conn:
                conn.sendall(
                    f"POST /exec HTTP/1.1\r\nAuthorization: Bearer tok\r\n"
                    f"Content-Length: {length}\r\n\r\n{{}}".encode()
                )
                assert conn.recv(100).startswith(b"HTTP/1.0 400"), length
    finally:
        server.shutdown()
        server.server_close()


def test_runner_expires_when_idle_or_too_old():
    server = runner.Server(("127.0.0.1", 0), "tok", idle_seconds=60, max_seconds=600)
    try:
        assert not server.expired()
        server.last_used -= 61
        assert server.expired()
        # Never idle while a call is running, but still bounded by its maximum lifetime.
        server.busy = 1
        assert not server.expired()
        server.started -= 601
        assert server.expired()
        expired = []
        runner.watch(server, on_expire=lambda: expired.append(True), interval=0)
        assert expired == [True]
    finally:
        server.server_close()


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to drop privileges")
def test_runner_runs_code_as_another_user_when_root(workdir):
    result = runner.execute("import os\nprint(os.getuid(), os.getgid())", 10)
    assert result["stdout"] == f"{runner.CHILD_UID} {runner.CHILD_UID}\n"
