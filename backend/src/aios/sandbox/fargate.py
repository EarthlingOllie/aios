"""Sandboxes as Fargate tasks: one task (its own Firecracker microVM) per agent run.

Each task runs the sandbox image's runner in `serve` mode. The API reaches it over the VPC at
the task's private IP, with a bearer token generated for the session and passed to the task as
an environment override. The task has no task role (no AWS credentials), a read-only root
filesystem, and a security group that only lets it reach the egress proxy. The runner exits
when idle or at its maximum lifetime, which stops the task.
"""

import asyncio
import logging
import secrets
import time
from typing import Any

import httpx
from anyio import CancelScope, to_thread
from botocore.exceptions import BotoCoreError, ClientError

from aios.config import Settings
from aios.sandbox.base import (
    ExecResult,
    SandboxError,
    SandboxGone,
    SandboxHandle,
    SandboxNotReached,
    SessionSpec,
)

log = logging.getLogger(__name__)

RUNNER_PORT = 8080
# How long to wait past a call's own time limit before giving up on the runner.
EXEC_GRACE = 30


class Unauthorized(SandboxGone):
    """The runner rejected the call's token. For a Fargate task that means another session's
    sandbox now has the address, so ours has stopped."""


class RunnerClient:
    """Calls a runner in `serve` mode (see `aios.sandbox.runner`)."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self.http = http or httpx.AsyncClient()

    async def healthy(self, endpoint: str, token: str, timeout: float = 3) -> bool:
        """Whether the runner at `endpoint` is up and holds `token`. Another session's
        runner can get the address once ours stops, and it refuses our token."""
        try:
            resp = await self.http.get(
                f"{endpoint}/health",
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def wait_healthy(
        self, endpoint: str, token: str, deadline: float, interval: float
    ) -> None:
        while not await self.healthy(endpoint, token):
            if time.monotonic() > deadline:
                raise SandboxError("The sandbox started but didn't respond in time.")
            await asyncio.sleep(interval)

    async def execute(self, handle: SandboxHandle, code: str, timeout: int) -> ExecResult:
        try:
            resp = await self.http.post(
                f"{handle.endpoint}/exec",
                json={"code": code, "timeout": timeout},
                headers={"Authorization": f"Bearer {handle.secret}"},
                timeout=httpx.Timeout(timeout + EXEC_GRACE, connect=5),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # No connection, so the request was never sent.
            raise SandboxNotReached("The sandbox had stopped, so the code didn't run.") from exc
        except httpx.HTTPError as exc:
            raise SandboxError("The sandbox didn't respond.") from exc
        if resp.status_code == 401:
            # Another session's sandbox now has the address: ours has stopped.
            raise Unauthorized("The sandbox has stopped.")
        if resp.status_code != 200:
            raise SandboxError(f"The sandbox refused the call ({resp.status_code}).")
        try:
            return ExecResult.model_validate(resp.json())
        except ValueError as exc:  # Bad JSON, or JSON that isn't a result (ValidationError).
            raise SandboxError("The sandbox sent back a result that couldn't be read.") from exc


def _private_ip(task: dict[str, Any]) -> str | None:
    for attachment in task.get("attachments", []):
        for detail in attachment.get("details", []):
            if detail.get("name") == "privateIPv4Address":
                return detail.get("value")
    return None


class FargateBackend:
    def __init__(
        self,
        settings: Settings,
        *,
        ecs: Any = None,
        runner: RunnerClient | None = None,
        poll_interval: float = 2,
    ) -> None:
        self.cluster = settings.sandbox_cluster
        self.task_definition = settings.sandbox_task_definition
        self.subnets = [s for s in settings.sandbox_subnets.split(",") if s]
        self.security_groups = [g for g in settings.sandbox_security_groups.split(",") if g]
        required = {
            "SANDBOX_CLUSTER": self.cluster,
            "SANDBOX_TASK_DEFINITION": self.task_definition,
            "SANDBOX_SUBNETS": self.subnets,
            "SANDBOX_SECURITY_GROUPS": self.security_groups,
        }
        if missing := [name for name, value in required.items() if not value]:
            # Caught here rather than as an opaque RunTask error on every call.
            log.error("the Fargate sandbox backend needs %s", ", ".join(missing))
            raise SandboxError(
                f"Running Python isn't set up correctly (missing {', '.join(missing)})."
            )
        if ecs is None:
            import boto3

            try:
                ecs = boto3.client("ecs")
            except BotoCoreError as exc:  # E.g. no region configured.
                log.warning("couldn't create the ECS client", exc_info=True)
                raise SandboxError(
                    f"Running Python isn't set up correctly ({type(exc).__name__})."
                ) from exc
        self.ecs = ecs
        self.runner = runner or RunnerClient()
        self.container = settings.sandbox_container
        self.start_timeout = settings.sandbox_start_timeout
        self.poll_interval = poll_interval

    async def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        # boto3 is blocking, so it runs on a worker thread. AWS errors (throttling, access
        # denied, missing credentials...) become SandboxErrors, which the tool reports to the
        # model instead of failing the run.
        try:
            return await to_thread.run_sync(lambda: getattr(self.ecs, method)(**kwargs))
        except (BotoCoreError, ClientError) as exc:
            # Only the error's name reaches the model: AWS messages can name roles and accounts.
            log.warning("ECS %s failed", method, exc_info=True)
            if isinstance(exc, ClientError):
                name = exc.response.get("Error", {}).get("Code") or "ClientError"
            else:
                name = type(exc).__name__
            raise SandboxError(f"The sandbox service failed ({name}).") from exc

    async def start(self, spec: SessionSpec) -> SandboxHandle:
        deadline = time.monotonic() + self.start_timeout
        token = secrets.token_urlsafe(32)
        environment = {
            "SANDBOX_TOKEN": token,
            "SANDBOX_PROXY_URL": spec.proxy_url,
            "SANDBOX_IDLE_SECONDS": str(spec.idle_seconds),
            "SANDBOX_MAX_SECONDS": str(spec.max_seconds),
        }
        resp = await self._call(
            "run_task",
            cluster=self.cluster,
            taskDefinition=self.task_definition,
            launchType="FARGATE",
            count=1,
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": self.subnets,
                    "securityGroups": self.security_groups,
                    "assignPublicIp": "DISABLED",
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": self.container,
                        "environment": [{"name": k, "value": v} for k, v in environment.items()],
                    }
                ]
            },
            startedBy=spec.session_key[:36],
            tags=[{"key": "aios:session", "value": spec.session_key}],
        )
        if not resp.get("tasks"):
            reasons = ", ".join(f.get("reason", "unknown") for f in resp.get("failures", []))
            raise SandboxError(f"Couldn't start a sandbox ({reasons or 'no task started'}).")
        arn = resp["tasks"][0]["taskArn"]
        try:
            ip = await self._wait_running(arn, deadline)
            endpoint = f"http://{ip}:{RUNNER_PORT}"
            await self.runner.wait_healthy(endpoint, token, deadline, self.poll_interval)
        except BaseException:
            # Shielded: if the start was cancelled (e.g. the client went away), anyio would
            # cancel the stop too, and the task would run until its runner's idle timeout.
            with CancelScope(shield=True):
                await self.stop(SandboxHandle(arn, "", ""))
            raise
        return SandboxHandle(arn, endpoint, token)

    async def _wait_running(self, arn: str, deadline: float) -> str:
        while True:
            resp = await self._call("describe_tasks", cluster=self.cluster, tasks=[arn])
            task = (resp.get("tasks") or [{}])[0]
            status = task.get("lastStatus")
            if status == "RUNNING" and (ip := _private_ip(task)):
                return ip
            if status in ("DEACTIVATING", "STOPPING", "DEPROVISIONING", "STOPPED"):
                reason = task.get("stoppedReason", "it stopped")
                raise SandboxError(f"The sandbox failed to start ({reason}).")
            if time.monotonic() > deadline:
                raise SandboxError("The sandbox took too long to start.")
            await asyncio.sleep(self.poll_interval)

    async def alive(self, handle: SandboxHandle) -> bool:
        return await self.runner.healthy(handle.endpoint, handle.secret)

    async def execute(self, handle: SandboxHandle, code: str, timeout: int) -> ExecResult:
        return await self.runner.execute(handle, code, timeout)

    async def stop(self, handle: SandboxHandle) -> None:
        try:
            await self._call(
                "stop_task", cluster=self.cluster, task=handle.ref, reason="Sandbox session ended"
            )
        except Exception:
            # Best effort: the runner stops itself when idle anyway.
            log.warning("couldn't stop sandbox task %s", handle.ref, exc_info=True)


class DevBackend:
    """Local development only: every run shares one long-lived runner (docker compose's
    `sandbox` service), with no isolation between runs and unrestricted network access."""

    def __init__(self, settings: Settings, *, runner: RunnerClient | None = None) -> None:
        self.endpoint = settings.sandbox_dev_url.rstrip("/")
        self.token = settings.sandbox_dev_token
        self.runner = runner or RunnerClient()

    async def start(self, spec: SessionSpec) -> SandboxHandle:
        if not await self.runner.healthy(self.endpoint, self.token):
            raise SandboxError(f"The dev sandbox at {self.endpoint} isn't running.")
        return SandboxHandle("dev", self.endpoint, self.token)

    async def alive(self, handle: SandboxHandle) -> bool:
        return await self.runner.healthy(handle.endpoint, handle.secret)

    async def execute(self, handle: SandboxHandle, code: str, timeout: int) -> ExecResult:
        try:
            return await self.runner.execute(handle, code, timeout)
        except Unauthorized:
            # The shared runner is still up; its token just isn't ours.
            raise SandboxError(
                f"The dev sandbox at {self.endpoint} rejected SANDBOX_DEV_TOKEN: set it to the"
                " runner's SANDBOX_TOKEN."
            ) from None

    async def stop(self, handle: SandboxHandle) -> None:
        pass
