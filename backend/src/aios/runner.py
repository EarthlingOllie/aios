"""Runs user-defined agents: a base prompt plus the skills attached to the agent."""

from pydantic_ai import Agent

from aios.config import get_settings
from aios.models import Agent as AgentDef

# One shared Pydantic AI agent; each run supplies the user-defined agent's instructions.
# No tools yet: an agent is a base prompt plus skills.
runner = Agent(get_settings().llm_model, name="custom-agent", defer_model_check=True)


def build_instructions(agent: AgentDef) -> str:
    sections = [agent.base_prompt.strip() or f"You are {agent.name}, a helpful assistant."]
    if agent.skills:
        sections.append(
            "You have the following skills. When a request matches a skill's description, "
            "follow that skill's instructions."
        )
        for skill in agent.skills:
            body = "\n\n".join(s for s in (skill.description.strip(), skill.content.strip()) if s)
            sections.append(f'<skill name="{skill.name}">\n{body}\n</skill>')
    return "\n\n".join(sections)
