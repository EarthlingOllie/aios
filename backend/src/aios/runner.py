"""Runs user-defined agents. Each run supplies its pinned AgentVersion's config."""

from langchain.agents import create_agent
from langgraph.checkpoint.base import BaseCheckpointSaver

from aios import llm
from aios.agent import ChatDeps
from aios.compaction import CompactionMiddleware
from aios.datastores.models import AgentVersion
from aios.jev import UNTRUSTED_RULE
from aios.model_rate_limit import RateLimitMiddleware
from aios.tool_checks import ToolResultChecks
from aios.tools import AgentTool, ToolOrganisation, build_tool
from aios.tools.web_search import WEB_SEARCH
from aios.usage import UsageMiddleware


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
    response_format=None,
):
    """A LangGraph agent for one turn of a run: its version's model, instructions and tools,
    the user and run as runtime context (`ChatDeps`), and the run's history in `checkpointer`.
    Its instructions end with what an untrusted block is, and every tool result reaches it
    checked by Jev and wrapped as one (`aios.tool_checks`). A long run's oldest turns reach it
    as a summary (`aios.compaction`). `response_format`, if given, is the
    structured output it gives (a workflow node's, `aios.workflows.structured.output_format`)."""
    return create_agent(
        llm.chat_model(model),
        server_tools(tools, model),
        system_prompt=f"{instructions}\n\n{UNTRUSTED_RULE}" if instructions else UNTRUSTED_RULE,
        # The run's latest summary in place of the turns it covers, folding in more once the
        # input passes the threshold (`aios.compaction`), before anything else sees the call.
        # Each tool result checked by Jev and wrapped as untrusted (`aios.tool_checks`).
        # And each model call charged to the turn and checked against its caps (`aios.usage`),
        # then paced by its model's rate limit (`aios.model_rate_limit`).
        middleware=[
            CompactionMiddleware(),
            ToolResultChecks(),
            UsageMiddleware(llm.model_name(model)),
            RateLimitMiddleware(llm.model_name(model)),
        ],
        context_schema=ChatDeps,
        checkpointer=checkpointer,
        response_format=response_format,
        name="custom-agent",
    )
