"""Runs user-defined agents. Each run supplies its pinned AgentVersion's config."""

from langchain.agents import create_agent
from langgraph.checkpoint.base import BaseCheckpointSaver

from aios import llm
from aios.agent import ChatDeps
from aios.datastores.models import AgentVersion
from aios.tools import AgentTool, ToolOrganisation, build_tool
from aios.tools.web_search import WEB_SEARCH


def server_tools(tools: list[AgentTool], model: str | None) -> list[AgentTool]:
    """The tools, with provider-run tools given the version the run's model supports.

    Web search alongside tools the agent calls itself gets the basic version. The dynamic one
    filters results in a server-side code execution that the model can start in the same
    response as a call to one of ours; the response then stops for our call with that
    execution unfinished, its result arrives in the next response, and the API refuses the
    history from then on (`code_execution` tool use without its result), failing the run.
    """
    dynamic = not any(not isinstance(t, dict) for t in tools)
    return [
        t | {"type": llm.web_search_type(model, dynamic=dynamic)}
        if isinstance(t, dict) and t.get("type") == WEB_SEARCH
        else t
        for t in tools
    ]


def version_tools(version: AgentVersion, org: ToolOrganisation) -> list[AgentTool]:
    """The version's tools, built from their stored settings to run in `org`. Raises `ToolError`
    if one can't be (see `aios.tools.build_tool`)."""
    return [tool for t in version.tools for tool in build_tool(t.tool_key, t.config, org)]


def run_title(text: str) -> str | None:
    """A run's title, from its first message."""
    return " ".join(text.split())[:120] or None


def build_agent(
    *,
    model: str | None,
    instructions: str,
    tools: list[AgentTool],
    checkpointer: BaseCheckpointSaver | None = None,
):
    """A LangGraph agent for one turn of a run: its version's model, instructions and tools,
    the user and run as runtime context (`ChatDeps`), and the run's history in `checkpointer`."""
    return create_agent(
        llm.chat_model(model),
        server_tools(tools, model),
        system_prompt=instructions,
        context_schema=ChatDeps,
        checkpointer=checkpointer,
        name="custom-agent",
    )
