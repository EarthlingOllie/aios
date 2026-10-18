import json

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, ConfigDict, Field

from aios.agent import ChatDeps
from aios.search import history_search_results
from aios.tools.registry import AgentTool, ToolSpec, register


class HistorySearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The same bounds as the document search's.
    max_results: int = Field(
        8,
        ge=1,
        le=50,
        title="Results per search",
        description="Messages returned for each search. More gives the model more context, and"
        " uses more tokens.",
    )


def build(config: HistorySearchConfig) -> list[AgentTool]:
    # The document search's retrieval, over the run history exported to the run's data store
    # (`aios.datastores.history`): by keyword always, and by meaning once embedding is on.
    @tool(parse_docstring=True)
    async def search_history(query: str, runtime: ToolRuntime[ChatDeps]) -> str:
        """Search the user's past runs (earlier conversations with their agents, including
        workflows' agent steps) with hybrid (semantic + keyword) retrieval.

        Each result is a message from a past run: who said it (user or assistant), its text,
        and the run it's from (its run_id, agent and title) with when the run started. Cite it
        as [run <run_id>, <date>].

        Args:
            query: A natural-language search query.
        """
        results = await history_search_results(
            data_store_id=runtime.context.data_store_id,
            query=query,
            limit=config.max_results,
            exclude_run_id=runtime.context.run_id,
        )
        # As JSON text: LangChain would pass a list through as content blocks.
        return json.dumps(results)

    return [search_history]


register(
    ToolSpec(
        key="history_search",
        name="Run history search",
        description="Search your past runs exported to your data store, combining semantic and"
        " keyword matching.",
        config_model=HistorySearchConfig,
        build=build,
    )
)
