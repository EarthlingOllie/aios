"""Sandboxes as gVisor containers on sandbox hosts we run: one container per agent run.

The API asks a broker (`aios.sandbox.broker`, one per host) for a session at SANDBOX_BROKER_URL,
the brokers' internal load balancer. The broker that answers starts the container and replies
with its own address, which the session records, so the run's later calls go straight to the
host its sandbox is on. A broker that no longer knows the session, or a host that refuses a
call's connection, means the sandbox has gone, and the session manager starts another. `alive()`,
which pre-warming asks on every chat turn, is stricter: only the broker saying the session is gone
counts, and any other failure raises SandboxError, so a brief network error or a busy broker
doesn't throw away the run's sandbox (and its /work).
"""

import asyncio
import logging

import httpx

from aios.config import Settings
from aios.sandbox.base import ExecResult, SandboxError, SandboxGone, SandboxHandle, SessionSpec

log = logging.getLogger(__name__)

# Stops of sandboxes whose start was cancelled (see `GVisorBackend.start`), kept so they aren't
# garbage-collected before they finish.
_cleanups: set[asyncio.Task] = set()

# How long to wait past a call's own time limit before giving up on the broker (which itself
# gives `docker exec` a little longer than the call's time limit).
EXEC_GRACE = 30
# The longest proxy token a session may have. It grows with the run's allowed domains; the
# egress proxy reads at most 8 KiB of request head, which holds the token base64-encoded again,
# so a much longer one couldn't be used anyway. Checked here before asking a broker, which
# enforces it too (`aios.sandbox.broker`).
PROXY_TOKEN_LIMIT = 4000
# Each broker only knows its own host's capacity: a start a full host refuses (503) is tried
# again, up to START_ATTEMPTS in all, on a new connection that may reach another host.
START_ATTEMPTS = 3
START_RETRY_DELAY = 0.5


def _detail(resp: httpx.Response) -> str | None:
    """The broker's model-readable explanation, if it gave one."""
    if resp.status_code == 401:
        return "The sandbox service refused this server's credentials."
    try:
        detail = resp.json().get("detail")
    except ValueError:
        return None
    return detail if isinstance(detail, str) else None


class GVisorBackend:
    def __init__(self, settings: Settings, *, http: httpx.AsyncClient | None = None) -> None:
        self.url = settings.sandbox_broker_url.rstrip("/")
        self.token = settings.sandbox_broker_token.get_secret_value()
        self.start_timeout = settings.sandbox_start_timeout
        self.http = http or httpx.AsyncClient()

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def start(self, spec: SessionSpec) -> SandboxHandle:
        if not self.url:
            raise SandboxError("Running Python isn't set up on this server.")
        # The proxy token carries the run's allowed domains, so a long enough list makes it
        # longer than the broker (and the egress proxy) accept.
        token = spec.proxy_url.partition("@")[0].rpartition(":")[2]
        if len(token) > PROXY_TOKEN_LIMIT:
            raise SandboxError(
                "This tool's list of reachable domains is too long for the sandbox; ask the"
                " agent's owner to shorten it."
            )
        # The request runs as a task of its own, so that if this start is cancelled (e.g. the
        # client went away) the broker's answer is still awaited, and a sandbox it started is
        # stopped: until `start` returns, nothing else knows it exists.
        request = asyncio.ensure_future(self._request_start(spec))
        try:
            return await asyncio.shield(request)
        except asyncio.CancelledError:
            cleanup = asyncio.ensure_future(self._stop_once_started(request))
            _cleanups.add(cleanup)
            cleanup.add_done_callback(_cleanups.discard)
            raise

    async def _stop_once_started(self, request: asyncio.Future[SandboxHandle]) -> None:
        try:
            handle = await request
        except Exception:
            return  # It didn't start one (or didn't say which, and the broker's reaper will).
        log.info("stopping sandbox %s: its start was cancelled", handle.ref)
        await self.stop(handle)

    async def _request_start(self, spec: SessionSpec) -> SandboxHandle:
        for attempt in range(1, START_ATTEMPTS + 1):
            try:
                resp = await self.http.post(
                    f"{self.url}/sessions",
                    json={
                        "session_key": spec.session_key,
                        "proxy_url": spec.proxy_url,
                        "idle_seconds": spec.idle_seconds,
                        "max_seconds": spec.max_seconds,
                    },
                    # A new connection each time: the load balancer picks a host per connection,
                    # so a retry can land on another one.
                    headers={**self._auth, "Connection": "close"},
                    timeout=httpx.Timeout(self.start_timeout, connect=5),
                )
            except httpx.HTTPError as exc:
                raise SandboxError("Couldn't reach the sandbox service.") from exc
            # 503: that host is full, but another may not be.
            if resp.status_code != 503 or attempt == START_ATTEMPTS:
                break
            log.info("sandbox host full (attempt %d); trying again", attempt)
            await asyncio.sleep(START_RETRY_DELAY * attempt)
        if resp.status_code != 201:
            raise SandboxError(_detail(resp) or f"Couldn't start a sandbox ({resp.status_code}).")
        try:
            body = resp.json()
            endpoint, ref = str(body["endpoint"]).rstrip("/"), str(body["id"])
        except (ValueError, TypeError, KeyError) as exc:
            raise SandboxError("The sandbox service gave an invalid answer.") from exc
        if not endpoint.startswith(("http://", "https://")):
            raise SandboxError("The sandbox service gave an invalid address.")
        # The broker token isn't stored with the session: calls use the current one, so it can
        # be rotated, and it stays out of the database.
        return SandboxHandle(ref=ref, endpoint=endpoint, secret="")

    async def alive(self, handle: SandboxHandle) -> bool:
        """True if the sandbox is running, False only if its broker says it has gone. Raises
        SandboxError when it can't tell (unreachable, timed out, or an unexpected answer): the
        sandbox may well still be there, and the next call finds out for sure."""
        try:
            resp = await self.http.get(
                f"{handle.endpoint}/sessions/{handle.ref}", headers=self._auth, timeout=5
            )
        except httpx.HTTPError as exc:
            raise SandboxError("Couldn't reach the sandbox.") from exc
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        raise SandboxError(_detail(resp) or f"Couldn't check the sandbox ({resp.status_code}).")

    async def execute(self, handle: SandboxHandle, code: str, timeout: int) -> ExecResult:
        try:
            resp = await self.http.post(
                f"{handle.endpoint}/sessions/{handle.ref}/exec",
                json={"code": code, "timeout": timeout},
                headers=self._auth,
                timeout=httpx.Timeout(timeout + EXEC_GRACE, connect=5),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # The host or its broker has gone, and its sandboxes with it.
            raise SandboxGone("The sandbox has stopped.") from exc
        except httpx.HTTPError as exc:
            raise SandboxError("The sandbox didn't respond.") from exc
        if resp.status_code == 404:
            raise SandboxGone(_detail(resp) or "The sandbox has stopped.")
        if resp.status_code != 200:
            raise SandboxError(
                _detail(resp) or f"The sandbox refused the call ({resp.status_code})."
            )
        try:
            return ExecResult.model_validate(resp.json())
        except ValueError as exc:  # Not JSON, or not a result (ValidationError).
            raise SandboxError("The sandbox gave an invalid answer.") from exc

    async def stop(self, handle: SandboxHandle) -> None:
        try:
            await self.http.delete(
                f"{handle.endpoint}/sessions/{handle.ref}", headers=self._auth, timeout=10
            )
        except httpx.HTTPError:
            # Best effort: the broker removes idle sandboxes anyway.
            log.warning("couldn't stop sandbox %s", handle.ref, exc_info=True)
