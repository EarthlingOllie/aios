from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import FunctionToolset, RunContext
from pydantic_ai.capabilities import Toolset

from aios.agent import ChatDeps
from aios.search import document_search_results
from aios.tools.registry import ToolSpec, register


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


def build(config: HybridSearchConfig) -> Toolset[ChatDeps]:
    # The same retrieval as the documents chat's `search_documents` tool, scoped to the documents
    # of the user running the agent (the run's deps).
    async def search_documents(ctx: RunContext[ChatDeps], query: str) -> list[dict]:
        """Search the user's uploaded documents with hybrid (semantic + keyword) retrieval.

        Each result has the passage's text, its source filename (cite it as [filename]) and the
        headings it sits under.

        Args:
            query: A natural-language search query.
        """
        return await document_search_results(
            owner_id=ctx.deps.user_id, query=query, limit=config.max_results
        )

    return Toolset(FunctionToolset([search_documents]))


register(
    ToolSpec(
        key="hybrid_search",
        name="Document search",
        description="Search your uploaded documents, combining semantic and keyword matching.",
        config_model=HybridSearchConfig,
        build=build,
    )
)
