"""Sandboxes as Lambda invocations: each `run_python` call is one invocation of the sandbox
function, and a session's /work lives in S3 between calls.

The function runs the sandbox image with `lambda_handler` (see that module). Every execution
environment is its own Firecracker microVM, and each session is invoked as its own Lambda tenant
(tenant isolation mode), so an environment only ever serves one session. Calls in a row usually
reuse a warm environment, which keeps /work locally; otherwise the handler restores it from S3.

A session is just an id and an expiry: there is nothing to keep running between calls, and
nothing is billed while a run is idle. Starting one invokes the function once (a "ping") to warm
an environment for its first call. A session ends at its maximum lifetime, when the proxy token it
was given expires; its idle time doesn't matter, because /work is kept in S3 meanwhile. Stopping
it deletes its /work.

The function reaches S3 through presigned URLs the API makes for each call (it has no S3 access of
its own), and the internet only through the egress proxy.
"""

import json
import logging
import secrets
import time
from collections.abc import Callable
from typing import Any

from anyio import to_thread
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from aios.config import Settings
from aios.sandbox.base import ExecResult, SandboxError, SandboxHandle, SessionSpec

log = logging.getLogger(__name__)

# How long a call's presigned URLs last past its own time limit: enough to restore /work before
# the code runs and save it afterwards.
URL_GRACE = 300
# The save form's size cap, past the unpacked /work limit the handler checks itself: tar headers
# on many small files can make the archive slightly bigger than the files.
ARCHIVE_SLACK = 16 * 1024 * 1024

FAILED_CALL = (
    "The sandbox stopped while running this code (for example, it ran out of memory or time), so"
    " the code may have partly run. /work is as it was before this call."
)


class _FunctionError(Exception):
    pass


def _expires(handle: SandboxHandle) -> float:
    try:
        return float(handle.endpoint.removeprefix("expires="))
    except ValueError:
        return 0


def work_key(session: str) -> str:
    return f"sessions/{session}/work.tar.gz"


class LambdaBackend:
    def __init__(
        self,
        settings: Settings,
        *,
        lambda_client: Any = None,
        s3_client: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.function = settings.sandbox_lambda_function
        self.bucket = settings.sandbox_work_bucket
        self.work_limit = settings.sandbox_work_limit_mb * 1024 * 1024
        self.clock = clock
        if not (self.function and self.bucket):
            raise SandboxError("Running Python isn't set up on this server.")
        if lambda_client is None or s3_client is None:
            lambda_client, s3_client = self._clients(lambda_client, s3_client)
        self.lam, self.s3 = lambda_client, s3_client

    @staticmethod
    def _clients(lambda_client: Any, s3_client: Any) -> tuple[Any, Any]:
        import boto3

        try:
            session = boto3.session.Session()
            region = session.region_name
            if not region:
                raise SandboxError("Running Python isn't set up correctly (no AWS region).")
            lambda_client = lambda_client or session.client(
                "lambda",
                # Never retried: a retry after the code started would run it twice. Long enough
                # for the longest call the function allows.
                config=Config(
                    connect_timeout=5, read_timeout=15 * 60, retries={"total_max_attempts": 1}
                ),
            )
            # Presigned URLs for the regional endpoint, which the sandbox subnets reach through
            # S3's gateway endpoint.
            s3_client = s3_client or session.client(
                "s3",
                endpoint_url=f"https://s3.{region}.amazonaws.com",
                config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
            )
        except BotoCoreError as exc:
            log.warning("couldn't create the AWS clients", exc_info=True)
            raise SandboxError(
                f"Running Python isn't set up correctly ({type(exc).__name__})."
            ) from exc
        return lambda_client, s3_client

    async def _call(self, work: Callable[[], Any]) -> Any:
        # boto3 is blocking, so it runs on a worker thread. AWS errors become SandboxErrors, which
        # the tool reports to the model instead of failing the run.
        try:
            return await to_thread.run_sync(work)
        except (BotoCoreError, ClientError) as exc:
            # Only the error's name reaches the model: AWS messages can name roles and accounts.
            log.warning("sandbox AWS call failed", exc_info=True)
            if isinstance(exc, ClientError):
                name = exc.response.get("Error", {}).get("Code") or "ClientError"
            else:
                name = type(exc).__name__
            if name in ("TooManyRequestsException", "ThrottlingException"):
                raise SandboxError("The sandbox service is busy; try again shortly.") from exc
            raise SandboxError(f"The sandbox service failed ({name}).") from exc

    async def _invoke(self, session: str, event: dict) -> dict:
        kwargs: dict[str, Any] = {
            "FunctionName": self.function,
            "InvocationType": "RequestResponse",
            "Payload": json.dumps(event).encode(),
            # Each session is a tenant of its own, so no execution environment serves two (the
            # function is created with tenant isolation, sandbox.tf).
            "TenantId": session,
        }

        def invoke() -> tuple[str | None, bytes]:
            resp = self.lam.invoke(**kwargs)
            return resp.get("FunctionError"), resp["Payload"].read()

        error, payload = await self._call(invoke)
        if error:
            # E.g. the function timed out, ran out of memory, or the code killed the handler.
            log.warning("sandbox invocation failed (%s): %s", error, payload[:500])
            raise _FunctionError(error)
        try:
            body = json.loads(payload)
        except ValueError as exc:
            raise SandboxError("The sandbox sent back an answer that couldn't be read.") from exc
        if not isinstance(body, dict):
            raise SandboxError("The sandbox sent back an answer that couldn't be read.")
        if isinstance(body.get("error"), str):
            raise SandboxError(body["error"])
        return body

    async def start(self, spec: SessionSpec) -> SandboxHandle:
        session = secrets.token_hex(16)
        expires = int(self.clock()) + spec.max_seconds
        try:
            # Warms an environment for this session's tenant, and checks the function is there.
            await self._invoke(session, {"ping": True})
        except _FunctionError as exc:
            raise SandboxError("Couldn't start a sandbox.") from exc
        log.info("sandbox session %s for %s", session, spec.session_key)
        # The proxy URL (with its token) goes with every call, so it's kept as the secret.
        return SandboxHandle(session, f"expires={expires}", spec.proxy_url)

    async def alive(self, handle: SandboxHandle) -> bool:
        # Nothing runs between calls, so only its lifetime can end a session (its proxy token
        # expires with it).
        return self.clock() < _expires(handle)

    async def execute(self, handle: SandboxHandle, code: str, timeout: int) -> ExecResult:
        if not await self.alive(handle):
            # Not SandboxGone, which the session manager reports as stopping partway through the
            # code: this is checked before anything runs.
            raise SandboxError(
                "The sandbox reached its time limit before this code ran, so it wasn't run. The"
                " next call starts a new sandbox, without the files in /work."
            )
        key, ttl = work_key(handle.ref), timeout + URL_GRACE

        def presign() -> tuple[str, dict]:
            restore = self.s3.generate_presigned_url(
                "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=ttl
            )
            save = self.s3.generate_presigned_post(
                self.bucket,
                key,
                Conditions=[["content-length-range", 0, self.work_limit + ARCHIVE_SLACK]],
                ExpiresIn=ttl,
            )
            return restore, save

        restore, save = await self._call(presign)
        event = {
            "session": handle.ref,
            "code": code,
            "timeout": timeout,
            "proxy_url": handle.secret,
            "work_limit": self.work_limit,
            "restore": restore,
            "save": save,
        }
        try:
            body = await self._invoke(handle.ref, event)
        except _FunctionError:
            raise SandboxError(FAILED_CALL) from None
        try:
            return ExecResult.model_validate(body.get("result"))
        except ValueError as exc:  # Not a result (ValidationError).
            raise SandboxError("The sandbox sent back a result that couldn't be read.") from exc

    async def stop(self, handle: SandboxHandle) -> None:
        # The session's environments stop on their own when idle; its /work is all that's left.
        try:
            await self._call(
                lambda: self.s3.delete_object(Bucket=self.bucket, Key=work_key(handle.ref))
            )
        except SandboxError:
            # Best effort: the bucket's lifecycle rule removes it anyway.
            log.warning("couldn't delete sandbox session %s's /work", handle.ref, exc_info=True)
