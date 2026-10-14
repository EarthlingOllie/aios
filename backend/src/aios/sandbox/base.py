"""What every sandbox backend implements, and the values passed across that boundary."""

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel


@dataclass(frozen=True)
class SessionSpec:
    """What a new sandbox is started with."""

    # E.g. `agent-run-42`, for the backend's tags, labels and logs.
    session_key: str
    # `http://sandbox:<token>@host:port`, the sandbox's only way out, or "" for no network.
    proxy_url: str
    # The sandbox stops itself after this long without a call, or this long in total, so a
    # crashed API or a deleted run can't leave one running.
    idle_seconds: int
    max_seconds: int


@dataclass(frozen=True)
class SandboxHandle:
    """How to reach a running sandbox. Stored on the run's `sandbox_sessions` row."""

    # The backend's id for it, e.g. a task ARN or a broker session id.
    ref: str
    # Where exec calls go.
    endpoint: str
    # Bearer token for exec calls.
    secret: str


class ExecResult(BaseModel):
    """One call's outcome, as the in-sandbox runner reports it (see `aios.sandbox.runner`)."""

    # None when the process was killed for running out of time.
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    # Whether stdout or stderr was cut at the runner's output limit.
    truncated: bool
    duration_ms: int


class SandboxError(RuntimeError):
    """A sandbox couldn't be started or reached. The message is shown to the model."""


class SandboxGone(SandboxError):
    """The session's sandbox no longer exists (stopped when idle, crashed, or expired)."""


class SandboxNotReached(SandboxGone):
    """The sandbox had gone before the call reached it, so the code didn't run."""


class SandboxBackend(Protocol):
    # Whether the sandbox's only way out is the proxy URL it's started with. False for the dev
    # backend, whose runner has unrestricted network, so it needs no proxy for allowed domains.
    restricts_network: bool

    async def start(self, spec: SessionSpec) -> SandboxHandle:
        """Starts a sandbox and returns once it can take calls.

        If it fails or is cancelled partway, it stops whatever it started: until it returns,
        nothing else knows the sandbox exists.
        """
        ...

    async def alive(self, handle: SandboxHandle) -> bool:
        """Whether the sandbox is still running and taking calls."""
        ...

    async def execute(self, handle: SandboxHandle, code: str, timeout: int) -> ExecResult:
        """Runs `code` in the sandbox. Raises `SandboxGone` if it no longer exists:
        `SandboxNotReached` when the call never reached it, so the code didn't run."""
        ...

    async def stop(self, handle: SandboxHandle) -> None:
        """Stops the sandbox, if it's still running."""
        ...
