"""Sandboxes as local Docker containers: one container per agent run. For local development.

Each sandbox is a container from the sandbox image (`docker build --target sandbox -t
aios-sandbox:local backend`) with a read-only root filesystem, tmpfs /work and /tmp, no
capabilities, an unprivileged user, and memory, CPU and process limits. It listens on nothing:
each call is a `docker exec` of the runner (`runner.py exec`) with the request on stdin. Its
main process is the runner too (`runner.py watch`), which exits, stopping the container, once
it has gone `idle_seconds` without a call or has run for `max_seconds`, so sandboxes don't
outlive a stopped API by long. A call never outlasts the maximum lifetime: the container stops
under it, and the call reports the sandbox gone.

It isolates far less than the Fargate or gVisor backends: the code shares the host's kernel
(runc), and a sandbox with allowed domains joins Docker's default bridge network, which has a
route out, so the egress proxy's allowlist only applies to code that uses the proxy (the
runner sets HTTPS_PROXY). One without allowed domains gets no network at all. Never use it in
production.
"""

import asyncio
import contextlib
import json
import logging
import os
import secrets

from anyio import CancelScope

from aios.config import Settings
from aios.sandbox.base import (
    ExecResult,
    SandboxError,
    SandboxGone,
    SandboxHandle,
    SandboxNotStarted,
    SessionSpec,
)

log = logging.getLogger(__name__)

RUNNER = "/opt/sandbox/runner.py"
# The image's unprivileged user (see the `sandbox` target in the Dockerfile).
SANDBOX_UID = 1001
LABEL = "aios.sandbox"
# How long `docker exec` may run past the call's own time limit.
EXEC_GRACE = 30
# How long other docker commands may take.
COMMAND_TIMEOUT = 60
# The runner caps stdout and stderr at about 20 KB each, so a bigger answer isn't its own.
MAX_ANSWER = 1024 * 1024
# How much of a command's stderr is kept (only for logs).
MAX_STDERR = 64 * 1024
# tmpfs sizes. Files on a tmpfs count against the container's memory limit, so the limit is the
# code's memory plus these: a full /work doesn't shrink the RAM the code gets.
WORK_MB = 512
TMP_MB = 64
# What `docker exec` says when the container had gone (removed, or stopped) before the exec.
NOT_STARTED = ("No such container", "is not running")


def cli_env() -> dict[str, str]:
    """The environment the docker CLI runs with: only what it needs to find itself and the
    daemon, not the API's secrets, which its plugins and credential helpers would see too.
    XDG_RUNTIME_DIR holds rootless Docker's socket. No HTTP(S)_PROXY: the CLI talks to a local
    daemon, and the sandbox never pulls (`--pull=never`)."""
    return {
        key: value
        for key, value in os.environ.items()
        if key in ("PATH", "HOME", "XDG_RUNTIME_DIR") or key.startswith("DOCKER_")
    }


class DockerBackend:
    def __init__(self, settings: Settings, *, docker: str = "docker") -> None:
        self.docker = docker
        self.image = settings.sandbox_docker_image
        self.memory_mb = settings.sandbox_docker_memory_mb
        self.start_timeout = settings.sandbox_start_timeout

    async def _run(
        self,
        *args: str,
        input: bytes | None = None,
        timeout: float = COMMAND_TIMEOUT,
        env: dict[str, str] | None = None,
    ) -> tuple[int, bytes, bytes]:
        """Runs `docker` with argv only (never a shell), in `cli_env()` plus `env`. Raises
        TimeoutError past `timeout`.

        Output past MAX_ANSWER (stdout) or MAX_STDERR kills the CLI and comes back cut off at
        one byte over the limit.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker,
                *args,
                stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**cli_env(), **(env or {})},
            )
        except OSError as exc:
            raise SandboxError(f"Docker isn't available here ({type(exc).__name__}).") from exc

        async def feed() -> None:
            if proc.stdin is None:
                return
            # The CLI may exit (or be killed) before reading it all.
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.write(input or b"")
                await proc.stdin.drain()
                proc.stdin.close()

        async def read(stream: asyncio.StreamReader, limit: int) -> bytes:
            # At most `limit` + 1 bytes, so the caller can tell output was cut off. Code in the
            # sandbox can write to the runner's stdout, so it must not grow the API's memory.
            data = b""
            while len(data) <= limit and (chunk := await stream.read(limit + 1 - len(data))):
                data += chunk
            if len(data) > limit:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            return data

        try:
            async with asyncio.timeout(timeout):
                _, stdout, stderr = await asyncio.gather(
                    feed(), read(proc.stdout, MAX_ANSWER), read(proc.stderr, MAX_STDERR)
                )
                await proc.wait()
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            # Shielded, so a cancelled call still reaps the CLI.
            with CancelScope(shield=True):
                await proc.wait()
            raise
        return proc.returncode or 0, stdout, stderr

    def run_args(self, name: str, spec: SessionSpec) -> list[str]:
        """`docker run` arguments for a session's sandbox."""
        network = (
            # Reaches a proxy on the host as host.docker.internal (as on Docker Desktop).
            ["--network=bridge", "--add-host=host.docker.internal:host-gateway"]
            if spec.proxy_url
            else ["--network=none"]
        )
        memory = f"{self.memory_mb + WORK_MB + TMP_MB}m"
        return [
            "run",
            "--detach",
            # Removed once the watcher exits, so stopped sandboxes don't pile up.
            "--rm",
            "--name",
            name,
            *network,
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={TMP_MB}m",
            "--tmpfs",
            f"/work:rw,nosuid,nodev,size={WORK_MB}m,uid={SANDBOX_UID},gid={SANDBOX_UID},mode=700",
            f"--user={SANDBOX_UID}:{SANDBOX_UID}",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--memory={memory}",
            f"--memory-swap={memory}",
            "--cpus=1",
            "--pids-limit=256",
            # Reaps the processes each call's code leaves behind (the runner kills them).
            "--init",
            "--pull=never",
            "--label",
            f"{LABEL}=1",
            "--label",
            f"{LABEL}.session={spec.session_key}",
            # The proxy URL holds the session's proxy token: passed by name, so it stays out of
            # argv (and `ps`).
            "--env",
            "SANDBOX_PROXY_URL",
            self.image,
            "python",
            RUNNER,
            "watch",
            str(spec.idle_seconds),
            str(spec.max_seconds),
        ]

    async def start(self, spec: SessionSpec) -> SandboxHandle:
        name = f"aios-sandbox-{secrets.token_hex(6)}"
        try:
            try:
                code, _, stderr = await self._run(
                    *self.run_args(name, spec),
                    timeout=self.start_timeout,
                    env={"SANDBOX_PROXY_URL": spec.proxy_url},
                )
            except TimeoutError:
                raise SandboxError("The sandbox took too long to start.") from None
            if code:
                detail = stderr.decode(errors="replace").strip()
                log.warning("couldn't start a sandbox for %s: %s", spec.session_key, detail[:500])
                if "No such image" in detail:
                    raise SandboxError(
                        f"The sandbox image {self.image} isn't built on this machine."
                    )
                raise SandboxError("Couldn't start a sandbox.")
        except BaseException:
            # Shielded: a timed-out or cancelled `docker run` only kills the CLI, and the daemon
            # may still create the container.
            with CancelScope(shield=True):
                await self.stop(SandboxHandle(name, "", ""))
            raise
        return SandboxHandle(name, "", "")

    async def alive(self, handle: SandboxHandle) -> bool:
        try:
            code, stdout, _ = await self._run(
                "inspect", "--format", "{{.State.Running}}", handle.ref
            )
        except (SandboxError, TimeoutError):
            return False
        return code == 0 and stdout.strip() == b"true"

    async def execute(self, handle: SandboxHandle, code: str, timeout: int) -> ExecResult:
        payload = json.dumps({"code": code, "timeout": timeout}).encode()
        try:
            status, stdout, stderr = await self._run(
                "exec",
                "--interactive",
                handle.ref,
                "python",
                RUNNER,
                "exec",
                input=payload,
                timeout=timeout + EXEC_GRACE,
            )
        except TimeoutError:
            # The runner enforces the call's time limit itself, so it has hung. Timing out only
            # kills the `docker exec` CLI: the runner would go on in the sandbox beside the next
            # call, in the same /work. So the sandbox goes, and the next call gets a fresh one.
            log.warning("runner in %s overran its time limit; removing it", handle.ref)
            with CancelScope(shield=True):
                await self.stop(handle)
            raise SandboxGone("The sandbox didn't finish the call in time.") from None
        except BaseException:
            # E.g. the agent run was stopped mid-call. Likewise, only the CLI is killed, so the
            # sandbox goes (shielded, so a cancelled call still removes it).
            with CancelScope(shield=True):
                await self.stop(handle)
            raise
        if status == 0 and len(stdout) <= MAX_ANSWER:
            try:
                return ExecResult.model_validate_json(stdout)
            except ValueError:
                pass
        detail = stderr.decode(errors="replace").strip()
        if not await self.alive(handle):
            if status and any(m in detail for m in NOT_STARTED):
                # The watchdog stopped it between the caller's check and the exec (idle or too
                # old), so the runner never started.
                raise SandboxNotStarted("The sandbox had stopped before the call.")
            raise SandboxGone("The sandbox has stopped.")
        log.warning(
            "runner failed in %s (exit %d, %d bytes of output): %s",
            handle.ref,
            status,
            len(stdout),
            detail[:500],
        )
        raise SandboxError("The sandbox couldn't run the code.")

    async def stop(self, handle: SandboxHandle) -> None:
        try:
            await self._run("rm", "--force", "--volumes", handle.ref)
        except Exception:
            # Best effort: the watcher stops it when idle anyway.
            log.warning("couldn't remove sandbox container %s", handle.ref, exc_info=True)
