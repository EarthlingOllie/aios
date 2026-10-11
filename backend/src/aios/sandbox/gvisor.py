"""Sandboxes as gVisor containers on sandbox hosts we run: one container per agent run.

The API asks a broker (`aios.sandbox.broker`, one per host) for a session at SANDBOX_BROKER_URL,
the brokers' internal load balancer. The broker that answers starts the container and replies
with its own address, which the session records, so the run's later calls go straight to the
host its sandbox is on. A host or broker that has gone (unreachable, or no longer knows the
session) means the sandbox has gone, and the session manager starts another.
"""

import logging

import httpx

from aios.config import Settings
from aios.sandbox.base import ExecResult, SandboxError, SandboxGone, SandboxHandle, SessionSpec

log = logging.getLogger(__name__)

# How long to wait past a call's own time limit before giving up on the broker (which itself
# gives `docker exec` a little longer than the call's time limit).
EXEC_GRACE = 30


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
        try:
            resp = await self.http.post(
                f"{self.url}/sessions",
                json={
                    "session_key": spec.session_key,
                    "proxy_url": spec.proxy_url,
                    "idle_seconds": spec.idle_seconds,
                    "max_seconds": spec.max_seconds,
                },
                headers=self._auth,
                timeout=httpx.Timeout(self.start_timeout, connect=5),
            )
        except httpx.HTTPError as exc:
            raise SandboxError("Couldn't reach the sandbox service.") from exc
        if resp.status_code != 201:
            raise SandboxError(_detail(resp) or f"Couldn't start a sandbox ({resp.status_code}).")
        body = resp.json()
        endpoint = str(body["endpoint"]).rstrip("/")
        if not endpoint.startswith(("http://", "https://")):
            raise SandboxError("The sandbox service gave an invalid address.")
        # The broker token isn't stored with the session: calls use the current one, so it can
        # be rotated, and it stays out of the database.
        return SandboxHandle(ref=str(body["id"]), endpoint=endpoint, secret="")

    async def alive(self, handle: SandboxHandle) -> bool:
        try:
            resp = await self.http.get(
                f"{handle.endpoint}/sessions/{handle.ref}", headers=self._auth, timeout=5
            )
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

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
        return ExecResult.model_validate(resp.json())

    async def stop(self, handle: SandboxHandle) -> None:
        try:
            await self.http.delete(
                f"{handle.endpoint}/sessions/{handle.ref}", headers=self._auth, timeout=10
            )
        except httpx.HTTPError:
            # Best effort: the broker removes idle sandboxes anyway.
            log.warning("couldn't stop sandbox %s", handle.ref, exc_info=True)
