"""Runs agent-written Python inside a sandbox.

This file is copied into the sandbox image on its own (see the `sandbox` target in the
Dockerfile), so it uses the standard library only and never imports `aios`.

    python runner.py exec    one call: reads {"code", "timeout"} as JSON on stdin and writes the
                             result as JSON on stdout (the gVisor broker runs it via docker exec)
    python runner.py serve   an HTTP server for calls: POST /exec, GET /health (Fargate). Needs
                             SANDBOX_TOKEN; exits when idle or at its maximum lifetime, which
                             stops the sandbox

Each call writes the code to a file and runs it as a new `python -I` process in the working
directory (/work, which persists between calls), in its own process group, with resource
limits and a minimal environment. The whole group is killed when the call ends or times out,
so background processes don't outlive their call. When the runner is root, the code runs as an
unprivileged user, so it can't read the runner's token or interfere with the runner.
"""

import contextlib
import hmac
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_TIMEOUT = 300
REQUEST_LIMIT = 1024 * 1024


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


WORKDIR = os.environ.get("SANDBOX_WORKDIR", "/work")
# Per stream (stdout, stderr), in bytes.
OUTPUT_LIMIT = _env_int("SANDBOX_OUTPUT_LIMIT", 20_000)
# Address space per call; 0 leaves it to the sandbox's own memory limit.
MEMORY_LIMIT_MB = _env_int("SANDBOX_MEMORY_LIMIT_MB", 0)
FILE_SIZE_LIMIT_MB = _env_int("SANDBOX_FILE_SIZE_LIMIT_MB", 1024)
CHILD_UID = _env_int("SANDBOX_CHILD_UID", 1001)


def child_env() -> dict[str, str]:
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": WORKDIR,
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MPLBACKEND": "Agg",
    }
    if proxy := os.environ.get("SANDBOX_PROXY_URL"):
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            env[name] = proxy
    return env


# Run in the child before the code: lowers its resource limits, then replaces itself with the
# code (Popen's `preexec_fn` isn't safe in a threaded server).
BOOTSTRAP = """
import json, os, resource, sys
for name, value in json.loads(sys.argv[1]).items():
    limit = getattr(resource, name)
    hard = resource.getrlimit(limit)[1]
    if hard != resource.RLIM_INFINITY:
        value = min(value, hard)
    resource.setrlimit(limit, (value, value))
os.execv(sys.executable, [sys.executable, "-I", sys.argv[2]])
"""


def limits(drop_privileges: bool) -> dict[str, int]:
    limits = {
        "RLIMIT_CORE": 0,
        "RLIMIT_NOFILE": 1024,
        "RLIMIT_FSIZE": FILE_SIZE_LIMIT_MB * 1024 * 1024,
    }
    if MEMORY_LIMIT_MB:
        limits["RLIMIT_AS"] = MEMORY_LIMIT_MB * 1024 * 1024
    if drop_privileges:
        # Counted per user, so only meaningful when the code runs as a user of its own.
        limits["RLIMIT_NPROC"] = 256
    return limits


class _Capture(threading.Thread):
    """Drains a pipe, keeping the first `limit` bytes, so a chatty child can't fill memory."""

    def __init__(self, pipe, limit: int) -> None:
        super().__init__(daemon=True)
        self.pipe, self.limit = pipe, limit
        self.data = bytearray()
        self.truncated = False
        self.start()

    def run(self) -> None:
        while chunk := self.pipe.read1(65536):
            room = self.limit - len(self.data)
            if len(chunk) > room:
                self.truncated = True
            if room > 0:
                self.data += chunk[:room]

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def execute(code: str, timeout: float) -> dict:
    timeout = max(1, min(float(timeout), MAX_TIMEOUT))
    drop = os.geteuid() == 0
    fd, path = tempfile.mkstemp(prefix="call-", suffix=".py")
    with os.fdopen(fd, "w") as f:
        f.write(code)
    os.chmod(path, 0o644)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", BOOTSTRAP, json.dumps(limits(drop)), path],
            cwd=WORKDIR,
            env=child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            **({"user": CHILD_UID, "group": CHILD_UID, "extra_groups": []} if drop else {}),
        )
        stdout = _Capture(proc.stdout, OUTPUT_LIMIT)
        stderr = _Capture(proc.stderr, OUTPUT_LIMIT)
        timed_out = False
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        # Also ends anything the code left running in the background, which would otherwise
        # hold the pipes open.
        _kill_group(proc.pid)
        proc.wait()
        stdout.join(5)
        stderr.join(5)
    finally:
        os.unlink(path)
    return {
        "exit_code": None if timed_out else proc.returncode,
        "stdout": stdout.text(),
        "stderr": stderr.text(),
        "timed_out": timed_out,
        "truncated": stdout.truncated or stderr.truncated,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def prepare_workdir() -> None:
    os.makedirs(WORKDIR, exist_ok=True)
    if os.geteuid() == 0:
        os.chown(WORKDIR, CHILD_UID, CHILD_UID)


# --- serve ---------------------------------------------------------------------------------------


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, token: str, idle_seconds: int, max_seconds: int) -> None:
        super().__init__(address, Handler)
        self.token = token
        self.idle_seconds, self.max_seconds = idle_seconds, max_seconds
        self.started = self.last_used = time.monotonic()
        self.busy = 0
        self.busy_lock = threading.Lock()
        self.exec_lock = threading.Lock()

    def expired(self) -> bool:
        now = time.monotonic()
        idle = not self.busy and now - self.last_used > self.idle_seconds
        return idle or now - self.started > self.max_seconds


class Handler(BaseHTTPRequestHandler):
    server: Server

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._reply(200, {"ok": True})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/exec":
            return self._reply(404, {"error": "not found"})
        auth = self.headers.get("Authorization", "")
        if not hmac.compare_digest(auth.encode(), f"Bearer {self.server.token}".encode()):
            return self._reply(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > REQUEST_LIMIT:
            return self._reply(413, {"error": "request too large"})
        try:
            body = json.loads(self.rfile.read(length))
            code, timeout = str(body["code"]), float(body.get("timeout", 60))
        except (ValueError, KeyError, TypeError):
            return self._reply(400, {"error": "expected {code, timeout}"})
        server = self.server
        with server.busy_lock:
            server.busy += 1
        try:
            # One call at a time: calls share /work.
            with server.exec_lock:
                result = execute(code, timeout)
        finally:
            with server.busy_lock:
                server.busy -= 1
                server.last_used = time.monotonic()
        self._reply(200, result)

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write(f"runner: {format % args}\n")


def watch(server: Server, on_expire=lambda: os._exit(0), interval: float = 5) -> None:
    """Calls `on_expire` (by default, exits, which stops the sandbox) once the server expires."""
    while not server.expired():
        time.sleep(interval)
    on_expire()


def serve() -> None:
    token = os.environ.pop("SANDBOX_TOKEN", "")
    if not token:
        raise SystemExit("SANDBOX_TOKEN is not set")
    prepare_workdir()
    server = Server(
        ("0.0.0.0", _env_int("SANDBOX_PORT", 8080)),
        token,
        idle_seconds=_env_int("SANDBOX_IDLE_SECONDS", 15 * 60),
        max_seconds=_env_int("SANDBOX_MAX_SECONDS", 2 * 60 * 60),
    )
    threading.Thread(target=watch, args=(server,), daemon=True).start()
    server.serve_forever()


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "exec":
        request = json.load(sys.stdin)
        prepare_workdir()
        json.dump(execute(str(request["code"]), float(request.get("timeout", 60))), sys.stdout)
    elif mode == "serve":
        serve()
    else:
        raise SystemExit("usage: runner.py exec|serve")


if __name__ == "__main__":
    main()
