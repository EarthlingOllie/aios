from functools import lru_cache

from aios.config import get_settings
from aios.sandbox.base import SandboxBackend, SandboxError


@lru_cache
def get_backend() -> SandboxBackend | None:
    """The backend `SANDBOX_BACKEND` names, or None if it's not set."""
    settings = get_settings()
    name = settings.sandbox_backend
    if not name:
        return None
    if name == "fargate":
        from aios.sandbox.fargate import FargateBackend

        return FargateBackend(settings)
    if name == "dev":
        from aios.sandbox.fargate import DevBackend

        return DevBackend(settings)
    raise SandboxError(f"Unknown sandbox backend: {name}")
