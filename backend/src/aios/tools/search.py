import json
from datetime import datetime

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, ConfigDict, Field

from aios.agent import ChatDeps
from aios.datastores.models import SearchItemKind
from aios.search import Filters, search_results
from aios.tools.registry import AgentTool, ToolSpec, register


class SearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The same bounds as GET /api/search.
    max_results: int = Field(
        8,
        ge=1,
        le=50,
        title="Results per search",
        description="Items returned for each search, at most. More gives the model more context,"
        " and uses more tokens.",
    )
    kinds: list[SearchItemKind] = Field(
        default_factory=lambda: list(SearchItemKind),
        min_length=1,
        title="What it searches",
        description="The kinds of item the agent may find: documents' passages, chat messages,"
        " past runs' messages and briefs.",
    )


def build(config: SearchConfig) -> list[AgentTool]:
    allowed = list(dict.fromkeys(config.kinds))

    # Hybrid retrieval over the search index of the run's data store (the run's context): every
    # item of the organisation's of the kinds allowed, but the run's own messages.
    @tool(parse_docstring=True)
    async def search(
        query: str,
        runtime: ToolRuntime[ChatDeps],
        kinds: list[SearchItemKind] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        chat_id: int | None = None,
        agent_id: int | None = None,
        limit: int | None = None,
    ) -> str:
        """Search everything in the organisation (documents, chat messages, past runs' messages
        and the briefs agents were given) with hybrid (semantic + keyword) retrieval.

        Each result has its kind, its text and when it was written, with where it's from: a
        document passage's source filename (cite it as [filename]) and headings; a past run's
        message's run_id, agent, title and role (cite it as [run <run_id>, <date>]); a chat
        message's or brief's chat_id and chat.

        Args:
            query: A natural-language search query.
            kinds: Only these kinds: document_chunk, chat_message, run_message, brief.
            since: Only items written at or after this time (ISO 8601).
            until: Only items written before this time (ISO 8601).
            chat_id: Only items from this chat.
            agent_id: Only items by or for this agent.
            limit: How many results, at most.
        """
        wanted = [k for k in (kinds or allowed) if k in allowed]
        if not wanted:
            return json.dumps([{"error": "This agent can't search those kinds of item."}])
        count = min(limit or config.max_results, config.max_results)
        results = await search_results(
            data_store_id=runtime.context.data_store_id,
            query=query,
            limit=max(count, 1),
            filters=Filters(
                kinds=wanted,
                since=since,
                until=until,
                chat_id=chat_id,
                agent_id=agent_id,
                exclude_run_id=runtime.context.run_id,
            ),
        )
        # As JSON text: LangChain would pass a list through as content blocks.
        return json.dumps(results)

    return [search]


register(
    ToolSpec(
        key="search",
        name="Search",
        description="Search everything in the organisation (documents, chat messages, past runs"
        " and briefs), combining semantic and keyword matching.",
        config_model=SearchConfig,
        build=build,
    )
)
