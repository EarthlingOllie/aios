"""The sandbox broker: runs on each gVisor sandbox host, and is the only thing that starts
containers there (`uvicorn aios.sandbox.broker:app --host 0.0.0.0 --port 8090`).

Each session is one container from the sandbox image, run under gVisor (`runsc`) with a
read-only root filesystem, tmpfs /work and /tmp, no capabilities, an unprivileged user, and
memory, CPU and process limits. It runs `sleep infinity` and listens on nothing: each call is a
`docker exec` of the runner (`runner.py exec`) with the request on stdin. A sandbox that may
reach the internet joins an internal Docker network (no route out) shared with the egress proxy
container, its only way out; one without allowed domains gets no network at all.

The broker holds the Docker socket, which is root on the host. So it runs `docker` with argv
lists only (never a shell), puts request data only into argv elements it has validated, names
containers with ids it generated itself, and only removes containers it labelled. Every call
needs the shared bearer token (SANDBOX_BROKER_TOKEN). Sessions live in memory: a restarted
broker removes the sandboxes it left behind, and the API treats their sessions as gone.
"""

import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Protocol

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from aios.config import Settings, get_settings
from aios.sandbox.base import ExecResult

log = logging.getLogger("aios.broker")

# Every sandbox container carries this label; nothing without it is ever removed.
LABEL = "aios.sandbox"
PROXY_NAME = "egress-proxy"
PROXY_PORT = 3128
RUNNER = "/opt/sandbox/runner.py"
# The sandbox image's `sandbox` user, and the api image's `app` user (for the proxy).
SANDBOX_UID = 1001
PROXY_UID = 1000
MAX_TIMEOUT = 300
REQUEST_LIMIT = 1024 * 1024
# The runner caps stdout and stderr at about 20 KB each, so a bigger answer isn't its own (the
# code runs as the same user as the runner, so it could write to the runner's stdout).
OUTPUT_LIMIT = 1024 * 1024
# How long `docker exec` may run past the call's own time limit.
EXEC_GRACE = 15
DOCKER_TIMEOUT = 60
REAP_INTERVAL = 15
IMDS = "http://169.254.169.254"

_SESSION_ID = re.compile(r"[0-9a-f]{32}")
_SESSION_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# `http://sandbox:<token>@host:port`, as `aios.sandbox.sessions` builds it.
_PROXY_URL = re.compile(
    r"http://[A-Za-z0-9._~%-]{1,64}:[A-Za-z0-9._~%-]{1,4000}@[a-z0-9.-]{1,253}:\d{1,5}"
)
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}")
_CONTAINER_NAME = re.compile(r"aios-sandbox-[0-9a-f]{32}")


# --- docker --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: bytes
    stderr: bytes


class OutputTooLarge(Exception):
    pass


class CommandRunner(Protocol):
    async def __call__(
        self,
        argv: list[str],
        *,
        input: bytes | None = None,
        timeout: float = DOCKER_TIMEOUT,
        env: dict[str, str] | None = None,
    ) -> Completed: ...


async def _read(stream: asyncio.StreamReader, limit: int) -> bytes:
    data = bytearray()
    while chunk := await stream.read(65536):
        data += chunk
        if len(data) > limit:
            raise OutputTooLarge
    return bytes(data)


async def run_command(
    argv: list[str],
    *,
    input: bytes | None = None,
    timeout: float = DOCKER_TIMEOUT,
    env: dict[str, str] | None = None,
) -> Completed:
    """Runs `argv` (never through a shell) with `input` on stdin and `env` added to the broker's
    environment. Kills it on timeout (TimeoutError) or past OUTPUT_LIMIT (OutputTooLarge)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **env} if env else None,
    )

    async def feed() -> None:
        if input is not None and proc.stdin is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.write(input)
                await proc.stdin.drain()
            proc.stdin.close()

    try:
        async with asyncio.timeout(timeout):
            _, stdout, stderr, _ = await asyncio.gather(
                feed(),
                _read(proc.stdout, OUTPUT_LIMIT),
                _read(proc.stderr, OUTPUT_LIMIT),
                proc.wait(),
            )
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    return Completed(proc.returncode or 0, stdout, stderr)


# --- sessions ------------------------------------------------------------------------------------


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_key: str
    proxy_url: str = ""
    idle_seconds: int = Field(ge=1, le=24 * 60 * 60)
    max_seconds: int = Field(ge=1, le=7 * 24 * 60 * 60)

    @field_validator("session_key")
    @classmethod
    def check_key(cls, value: str) -> str:
        if not _SESSION_KEY.fullmatch(value):
            raise ValueError("expected letters, digits, '.', '_' or '-' (at most 64)")
        return value

    @field_validator("proxy_url")
    @classmethod
    def check_proxy(cls, value: str) -> str:
        if value and not _PROXY_URL.fullmatch(value):
            raise ValueError("expected http://user:token@host:port")
        return value


class ExecRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    timeout: int = Field(60, ge=1, le=MAX_TIMEOUT)


@dataclass
class Session:
    id: str
    container: str
    key: str
    idle_seconds: int
    max_seconds: int
    started: float
    last_used: float
    # Calls running or waiting; a busy session is never idle.
    busy: int = 0
    # One call at a time: calls share /work.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def expired(self, now: float) -> bool:
        idle = not self.busy and now - self.last_used > self.idle_seconds
        return idle or now - self.started > self.max_seconds


class BrokerError(Exception):
    """Answered as `status` with `detail`, which the API shows to the model."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


GONE = "The sandbox has stopped."


class Broker:
    def __init__(
        self,
        settings: Settings,
        *,
        docker: CommandRunner = run_command,
        clock: Callable[[], float] = time.monotonic,
        imds: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.token = settings.sandbox_broker_token.get_secret_value()
        self.endpoint = settings.sandbox_broker_advertise_url.rstrip("/")
        self.sessions: dict[str, Session] = {}
        self._run = docker
        self._clock = clock
        self._imds = imds

    async def _docker(self, *args: str, **kwargs) -> Completed:
        return await self._run(["docker", *args], **kwargs)

    async def _check(self, *args: str, **kwargs) -> Completed:
        done = await self._docker(*args, **kwargs)
        if done.returncode != 0:
            stderr = done.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"docker {args[0]} failed: {stderr}")
        return done

    # --- startup ---

    async def startup(self) -> None:
        if not self.token:
            raise RuntimeError("SANDBOX_BROKER_TOKEN is not set")
        if not self.endpoint:
            self.endpoint = await self._advertise_url()
        await self._remove_leftovers()
        await self._ensure_network()
        await self._start_proxy()
        log.info("broker ready at %s", self.endpoint)

    async def _advertise_url(self) -> str:
        """http://<this instance's private IP>:<port>, from the metadata service (IMDSv2)."""
        async with self._imds or httpx.AsyncClient(timeout=5) as http:
            resp = await http.put(
                f"{IMDS}/latest/api/token", headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}
            )
            resp.raise_for_status()
            resp = await http.get(
                f"{IMDS}/latest/meta-data/local-ipv4",
                headers={"X-aws-ec2-metadata-token": resp.text},
            )
            resp.raise_for_status()
        ip = ipaddress.IPv4Address(resp.text.strip())
        return f"http://{ip}:{self.settings.sandbox_broker_port}"

    async def _remove_leftovers(self) -> None:
        """Removes sandboxes a previous broker left: their sessions were lost with it."""
        done = await self._check(
            "ps", "--all", "--quiet", "--no-trunc", "--filter", f"label={LABEL}=1"
        )
        ids = [i for i in done.stdout.decode().split() if _CONTAINER_ID.fullmatch(i)]
        if ids:
            log.info("removing %d sandboxes left by a previous broker", len(ids))
            await self._check("rm", "--force", "--volumes", *ids)

    async def _ensure_network(self) -> None:
        network = self.settings.sandbox_network
        done = await self._docker("network", "inspect", "--format", "{{.Internal}}", network)
        if done.returncode == 0:
            # Sandboxes rely on it having no route out.
            if done.stdout.strip() != b"true":
                raise RuntimeError(f"Docker network {network} exists but isn't --internal")
            return
        await self._check(
            "network",
            "create",
            "--driver=bridge",
            "--internal",
            f"--subnet={self.settings.sandbox_network_subnet}",
            "--label",
            f"{LABEL}.network=1",
            network,
        )

    async def _start_proxy(self) -> None:
        """(Re)creates the egress proxy, on the default bridge (its way out) and the sandbox
        network, where sandboxes find it as `egress-proxy:3128`."""
        secret = self.settings.sandbox_proxy_secret.get_secret_value()
        await self._docker("rm", "--force", PROXY_NAME)
        if not secret:
            log.warning("SANDBOX_PROXY_SECRET is not set: sandboxes get no network access")
            return
        await self._check(
            "run",
            "--detach",
            "--name",
            PROXY_NAME,
            "--restart=unless-stopped",
            "--runtime=runc",
            "--network=bridge",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--user={PROXY_UID}:{PROXY_UID}",
            "--memory=256m",
            "--pids-limit=256",
            "--pull=never",
            "--label",
            "aios.egress-proxy=1",
            # Passed through the environment, not argv, so it doesn't show up in `ps`.
            "--env",
            "SANDBOX_PROXY_SECRET",
            self.settings.sandbox_proxy_image,
            "python",
            "-m",
            "aios.sandbox.egress_proxy",
            "--port",
            str(PROXY_PORT),
            env={"SANDBOX_PROXY_SECRET": secret},
        )
        await self._check(
            "network", "connect", "--alias", PROXY_NAME, self.settings.sandbox_network, PROXY_NAME
        )

    # --- sessions ---

    def run_args(self, session: Session, proxy_url: str) -> list[str]:
        """`docker run` arguments for a session's sandbox."""
        s = self.settings
        return [
            "run",
            "--detach",
            "--name",
            session.container,
            f"--runtime={s.sandbox_docker_runtime}",
            # The internal network only reaches the proxy; without domains, no network at all.
            f"--network={s.sandbox_network if proxy_url else 'none'}",
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={s.sandbox_tmp_mb}m",
            "--tmpfs",
            f"/work:rw,nosuid,nodev,size={s.sandbox_work_mb}m,"
            f"uid={SANDBOX_UID},gid={SANDBOX_UID},mode=700",
            f"--user={SANDBOX_UID}:{SANDBOX_UID}",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--memory={s.sandbox_memory_mb}m",
            f"--memory-swap={s.sandbox_memory_mb}m",
            f"--cpus={s.sandbox_cpus:g}",
            f"--pids-limit={s.sandbox_pids_limit}",
            # Reaps the processes each call's code leaves behind (the runner kills them).
            "--init",
            "--log-driver=none",
            "--pull=never",
            "--label",
            f"{LABEL}=1",
            "--label",
            f"aios.session={session.key}",
            # The proxy URL holds the session's proxy token: kept out of argv, as above.
            "--env",
            "SANDBOX_PROXY_URL",
            s.sandbox_image,
            "sleep",
            "infinity",
        ]

    def get(self, session_id: str) -> Session:
        session = self.sessions.get(session_id) if _SESSION_ID.fullmatch(session_id) else None
        if session is None:
            raise BrokerError(404, GONE)
        return session

    async def _remove(self, session: Session) -> None:
        if self.sessions.get(session.id) is session:
            del self.sessions[session.id]
        try:
            done = await self._docker("rm", "--force", "--volumes", session.container)
        except (TimeoutError, OutputTooLarge, OSError) as exc:
            done = Completed(-1, b"", repr(exc).encode())
        if done.returncode != 0:
            log.warning("couldn't remove %s: %s", session.container, done.stderr[:500])

    async def _running(self, session: Session) -> bool:
        try:
            done = await self._docker(
                "inspect", "--type=container", "--format", "{{.State.Running}}", session.container
            )
        except (TimeoutError, OutputTooLarge, OSError):
            return False
        return done.returncode == 0 and done.stdout.strip() == b"true"

    async def start(self, request: StartRequest) -> Session:
        if len(self.sessions) >= self.settings.sandbox_max_sessions:
            raise BrokerError(503, "No sandbox capacity right now; try again shortly.")
        session_id = secrets.token_hex(16)
        now = self._clock()
        session = Session(
            id=session_id,
            container=f"aios-sandbox-{session_id}",
            key=request.session_key,
            idle_seconds=request.idle_seconds,
            max_seconds=request.max_seconds,
            started=now,
            last_used=now,
            busy=1,
        )
        # Recorded first, so it counts towards the limit while it starts.
        self.sessions[session_id] = session
        try:
            done = await self._docker(
                *self.run_args(session, request.proxy_url),
                env={"SANDBOX_PROXY_URL": request.proxy_url},
            )
            failure = done.stderr.decode(errors="replace").strip() if done.returncode else ""
        except (TimeoutError, OutputTooLarge, OSError) as exc:
            failure = repr(exc)
        finally:
            session.busy = 0
        if failure or self.sessions.get(session_id) is not session:
            log.warning("couldn't start a sandbox for %s: %s", session.key, failure[:500])
            await self._remove(session)
            raise BrokerError(502, "Couldn't start a sandbox.")
        log.info("started %s for %s", session.container, session.key)
        return session

    async def alive(self, session_id: str) -> None:
        session = self.get(session_id)
        if not await self._running(session):
            await self._remove(session)
            raise BrokerError(404, GONE)

    async def execute(self, session_id: str, request: ExecRequest) -> dict:
        session = self.get(session_id)
        session.busy += 1
        try:
            async with session.lock:
                if self.sessions.get(session_id) is not session:
                    raise BrokerError(404, GONE)
                return await self._execute(session, request)
        finally:
            session.busy -= 1
            session.last_used = self._clock()

    async def _execute(self, session: Session, request: ExecRequest) -> dict:
        payload = json.dumps({"code": request.code, "timeout": request.timeout}).encode()
        try:
            done = await self._docker(
                "exec",
                "--interactive",
                session.container,
                "python",
                RUNNER,
                "exec",
                input=payload,
                timeout=request.timeout + EXEC_GRACE,
            )
        except TimeoutError:
            raise BrokerError(504, "The sandbox didn't finish the call in time.") from None
        except OutputTooLarge:
            raise BrokerError(502, "The sandbox's answer was too large.") from None
        if done.returncode == 0:
            try:
                return ExecResult.model_validate_json(done.stdout).model_dump()
            except ValidationError:
                pass
        if not await self._running(session):
            await self._remove(session)
            raise BrokerError(404, GONE)
        log.warning("runner failed in %s: %s", session.container, done.stderr[:500])
        raise BrokerError(502, "The sandbox couldn't run the code.")

    async def stop(self, session_id: str) -> None:
        await self._remove(self.get(session_id))

    async def reap(self) -> list[str]:
        """Removes sandboxes idle for longer than their idle time, or older than their maximum
        lifetime, and any sandbox container without a session. Returns the expired sessions'
        ids."""
        now = self._clock()
        expired = [s for s in self.sessions.values() if s.expired(now)]
        for session in expired:
            log.info("removing %s (%s): idle or expired", session.container, session.key)
            await self._remove(session)
        await self._remove_orphans()
        return [s.id for s in expired]

    async def _remove_orphans(self) -> None:
        """Removes sandbox containers no session owns: ones whose `docker rm` failed, or that the
        daemon went on to create after a timed-out `docker run` (which only kills the CLI), and
        so after its session's `docker rm` had already run. A container that is starting has its
        session already, so it's never one of these."""
        done = await self._check(
            "ps", "--all", "--filter", f"label={LABEL}=1", "--format", "{{.Names}}"
        )
        owned = {s.container for s in self.sessions.values()}
        orphans = [
            name
            for name in done.stdout.decode().split()
            if _CONTAINER_NAME.fullmatch(name) and name not in owned
        ]
        if orphans:
            log.warning("removing %d sandboxes without a session", len(orphans))
            await self._check("rm", "--force", "--volumes", *orphans)

    async def reap_forever(self, interval: float = REAP_INTERVAL) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.reap()
            except Exception:
                log.exception("reaping failed")


# --- HTTP API ------------------------------------------------------------------------------------


async def _body[M: BaseModel](request: Request, model: type[M]) -> M:
    """The request body as `model`, refusing more than REQUEST_LIMIT bytes."""
    length = request.headers.get("content-length", "")
    if length and (not length.isdigit() or int(length) > REQUEST_LIMIT):
        raise HTTPException(413, "request too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > REQUEST_LIMIT:
            raise HTTPException(413, "request too large")
    try:
        return model.model_validate_json(bytes(body))
    except ValidationError as exc:
        detail = exc.errors(include_url=False, include_context=False, include_input=False)
        raise HTTPException(422, detail) from None


def _broker(request: Request, authorization: Annotated[str, Header()] = "") -> Broker:
    broker: Broker = request.app.state.broker
    expected = f"Bearer {broker.token}".encode()
    if not broker.token or not hmac.compare_digest(authorization.encode(), expected):
        raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
    return broker


Authorized = Annotated[Broker, Depends(_broker)]


def create_app(broker: Broker | None = None) -> FastAPI:
    """The broker's app. Without `broker`, one is built from the settings at startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if broker is None:
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
            app.state.broker = Broker(get_settings())
        await app.state.broker.startup()
        reaper = asyncio.create_task(app.state.broker.reap_forever())
        try:
            yield
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper

    app = FastAPI(
        title="aios sandbox broker",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.broker = broker

    @app.exception_handler(BrokerError)
    async def broker_error(request: Request, exc: BrokerError) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    # For the load balancer's health checks: no token, and says nothing about sessions.
    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.post("/sessions", status_code=201)
    async def start(request: Request, broker: Authorized) -> dict:
        session = await broker.start(await _body(request, StartRequest))
        return {"id": session.id, "endpoint": broker.endpoint}

    @app.get("/sessions/{session_id}")
    async def alive(session_id: str, broker: Authorized) -> dict:
        await broker.alive(session_id)
        return {"id": session_id}

    @app.post("/sessions/{session_id}/exec")
    async def execute(session_id: str, request: Request, broker: Authorized) -> dict:
        broker.get(session_id)
        return await broker.execute(session_id, await _body(request, ExecRequest))

    @app.delete("/sessions/{session_id}", status_code=204)
    async def stop(session_id: str, broker: Authorized) -> Response:
        await broker.stop(session_id)
        return Response(status_code=204)

    return app


app = create_app()
