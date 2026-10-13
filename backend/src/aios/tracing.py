"""Tracing agent runs to Langfuse (self-hosted or cloud), when LANGFUSE_* settings are set."""

from functools import lru_cache
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler

from aios.config import get_settings


@lru_cache
def _client() -> Langfuse | None:
    settings = get_settings()
    secret = settings.langfuse_secret_key.get_secret_value()
    if not (settings.langfuse_public_key and secret):
        return None
    # Registered with Langfuse under its public key; the callback handlers below find it by that.
    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=secret,
        base_url=settings.langfuse_base_url,
        environment=settings.langfuse_environment or None,
    )


def callbacks() -> list[BaseCallbackHandler]:
    """Callbacks that trace a LangGraph run, or none if tracing isn't configured."""
    if _client() is None:
        return []
    return [CallbackHandler(public_key=get_settings().langfuse_public_key)]


def trace_metadata(
    *, session_id: str | None, user_id: int, tags: list[str], **metadata: Any
) -> dict[str, Any]:
    """Run metadata, with the keys Langfuse reads its session, user and tags from."""
    out = {**metadata, "langfuse_user_id": str(user_id), "langfuse_tags": tags}
    if session_id:
        out["langfuse_session_id"] = session_id
    return out


def flush() -> None:
    """Send buffered traces (they're exported in the background); called on shutdown."""
    client = _client()
    if client is not None:
        client.flush()
