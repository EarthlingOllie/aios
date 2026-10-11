from dataclasses import dataclass

from pydantic_ai import Agent, RunContext

from aios.config import get_settings
from aios.search import document_search_results


@dataclass
class ChatDeps:
    # Also the deps of user-defined agents' runs (`aios.runner`), which tools read the user from.
    user_id: int
    # The agent run (user-defined agents only), e.g. for the run's sandbox (`run_python`).
    run_id: int | None = None


agent = Agent(
    get_settings().llm_model,
    deps_type=ChatDeps,
    # Don't require provider API keys at import time (tests, migrations, the worker).
    defer_model_check=True,
    instructions=(
        "You are a helpful assistant with access to the user's uploaded documents. "
        "When a question may be answered by those documents, call `search_documents` first "
        "and ground your answer in the results, citing sources as [filename]. "
        "If the documents don't contain the answer, say so plainly."
    ),
)


@agent.tool
async def search_documents(ctx: RunContext[ChatDeps], query: str) -> list[dict]:
    """Search the user's documents with hybrid (semantic + keyword) retrieval.

    Args:
        query: A natural-language search query.
    """
    return await document_search_results(owner_id=ctx.deps.user_id, query=query)
