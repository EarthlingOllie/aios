import json
import uuid
from dataclasses import dataclass

from langchain.agents import create_agent
from langchain.tools import ToolRuntime, tool

from aios import llm
from aios.search import document_search_results


@dataclass
class ChatDeps:
    """The runtime context of an agent run (LangGraph `context_schema`); tools read it."""

    # The user the run is for. Also the context of user-defined agents' runs (`aios.runner`).
    user_id: int
    # The agent run (user-defined agents only), e.g. for the run's sandbox (`run_python`).
    run_id: int | None = None
    # The data store the run's tools read and write (`aios.datastores`), resolved when the run
    # or chat turn starts. Tools take it rather than the user, so they needn't change when a
    # run's store isn't its user's (teams).
    data_store_id: uuid.UUID | None = None
    # The workflow run the agent run is a node of, if it is one: such a run can't wait for a
    # schema change request to be decided yet (`request_schema_change`).
    workflow_run_id: int | None = None


INSTRUCTIONS = (
    "You are a helpful assistant with access to the user's uploaded documents. "
    "When a question may be answered by those documents, call `search_documents` first "
    "and ground your answer in the results, citing sources as [filename]. "
    "If the documents don't contain the answer, say so plainly."
)


@tool(parse_docstring=True)
async def search_documents(query: str, runtime: ToolRuntime[ChatDeps]) -> str:
    """Search the user's documents with hybrid (semantic + keyword) retrieval.

    Args:
        query: A natural-language search query.
    """
    # As JSON text: LangChain would pass a list through as content blocks.
    results = await document_search_results(
        data_store_id=runtime.context.data_store_id, query=query
    )
    return json.dumps(results)


def documents_agent():
    """The documents chat's agent. Stateless: the client sends the whole conversation."""
    return create_agent(
        llm.chat_model(),
        [search_documents],
        system_prompt=INSTRUCTIONS,
        context_schema=ChatDeps,
        name="documents-chat",
    )
