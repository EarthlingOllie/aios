"""Tracing agent runs to Langfuse (self-hosted or cloud), when LANGFUSE_* settings are set."""

from functools import lru_cache
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler

from aios.config import get_settings
from aios.models import AgentVersion


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


def agent_version_metadata(version: AgentVersion) -> dict[str, Any]:
    """The agent version's part of a run's metadata: the same whether the run is a chat or a
    workflow's agent node, so filters on e.g. its skills or tools find both."""
    return {
        "agent_id": version.agent_id,
        "agent_name": version.agent.name,
        "agent_version": version.version,
        "agent_version_id": version.id,
        "skill_versions": [
            {"skill_id": sv.skill_id, "name": sv.name, "version": sv.version, "id": sv.id}
            for sv in version.skills
        ],
        "tools": [{"key": t.tool_key, "config": t.config} for t in version.tools],
    }


def flush() -> None:
    """Send buffered traces (they're exported in the background); called on shutdown."""
    client = _client()
    if client is not None:
        client.flush()
