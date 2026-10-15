import json

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, ConfigDict, Field

from aios.agent import ChatDeps
from aios.search import document_search_results
from aios.tools.registry import AgentTool, ToolSpec, register


class HybridSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The same bounds as GET /api/search.
    max_results: int = Field(
        8,
        ge=1,
        le=50,
        title="Results per search",
        description="Passages returned for each search. More gives the model more context, and"
        " uses more tokens.",
    )


def build(config: HybridSearchConfig) -> list[AgentTool]:
    # The same retrieval as the documents chat's `search_documents` tool, over the documents in
    # the run's data store (the run's context).
    @tool(parse_docstring=True)
    async def search_documents(query: str, runtime: ToolRuntime[ChatDeps]) -> str:
        """Search the user's uploaded documents with hybrid (semantic + keyword) retrieval.

        Each result has the passage's text, its source filename (cite it as [filename]) and the
        headings it sits under.

        Args:
            query: A natural-language search query.
        """
        results = await document_search_results(
            data_store_id=runtime.context.data_store_id, query=query, limit=config.max_results
        )
        # As JSON text: LangChain would pass a list through as content blocks.
        return json.dumps(results)

    return [search_documents]


register(
    ToolSpec(
        key="hybrid_search",
        name="Document search",
        description="Search your uploaded documents, combining semantic and keyword matching.",
        config_model=HybridSearchConfig,
        build=build,
    )
)
