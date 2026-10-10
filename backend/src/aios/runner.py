"""Runs user-defined agents. Each run supplies its pinned AgentVersion's config."""

from pydantic_ai import Agent

from aios.agent import ChatDeps
from aios.config import get_settings

# One shared Pydantic AI agent. Each run passes its agent version's instructions, model and
# tools (built from the tool library, `aios.tools`), and the user running it as deps.
runner = Agent(
    get_settings().llm_model, name="custom-agent", deps_type=ChatDeps, defer_model_check=True
)
