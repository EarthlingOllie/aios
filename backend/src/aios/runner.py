"""Runs user-defined agents. Each run supplies its pinned AgentVersion's config."""

from pydantic_ai import Agent

from aios.config import get_settings

# One shared Pydantic AI agent. Each run passes its agent version's instructions, model and
# tools (built from the tool library, `aios.tools`).
runner = Agent(get_settings().llm_model, name="custom-agent", defer_model_check=True)
