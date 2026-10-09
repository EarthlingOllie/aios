"""Runs user-defined agents. Each run supplies its pinned AgentVersion's instructions."""

from pydantic_ai import Agent

from aios.config import get_settings

# One shared Pydantic AI agent; no tools yet (an agent is a base prompt plus skills).
runner = Agent(get_settings().llm_model, name="custom-agent", defer_model_check=True)
