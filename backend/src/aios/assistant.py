"""The documents assistant: a built-in agent that answers from the documents, and joins each new
chat (`aios.api.chats.create_chat`).

It's an ordinary agent of the organisation's, in its store, one per organisation, made by the app
for whichever member first needs it (who is its `created_by`), with the document search tool
(`hybrid_search`) and instructions to ground its answers in what it finds. Being an agent,
mentioning it in a chat runs a turn of it like any agent member (with the acknowledgement,
brief, card and posted reply, `aios.invocations`). Any member can change it as they would any
of the organisation's agents; `builtin` is how it's found again, whatever it's been renamed.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aios.api.provenance import built_by
from aios.datastores.models import Agent
from aios.tools import validate_tool
from aios.versioning import save_agent_version

BUILTIN = "documents"
NAME = "Documents assistant"
DESCRIPTION = "Answers from the organisation's documents. Built in: it joins each new chat."
# The transaction-level advisory lock that making it takes, in the organisation's store (a lock
# for the transaction only, which a connection pooler's transactions can hold).
LOCK = 0x61696F73_646F6373  # "aiosdocs"
INSTRUCTIONS = (
    "You are a helpful assistant with access to the organisation's uploaded documents. "
    "When a question may be answered by those documents, call `search_documents` first "
    "and ground your answer in the results, citing sources as [filename]. "
    "If the documents don't contain the answer, say so plainly."
)


def free_name(store: Session) -> str:
    """`NAME`, or the first of "NAME 2", "NAME 3"… none of the organisation's agents (in its
    store) is called (an archived agent's name stays reserved)."""
    taken = set(store.scalars(select(Agent.name).where(Agent.name.startswith(NAME))))
    n = 1
    while (name := NAME if n == 1 else f"{NAME} {n}") in taken:
        n += 1
    return name


def documents_assistant(store: Session, user_id: int) -> Agent:
    """The organisation's documents assistant, in its store (`store`), made by `user_id` (the
    member who needs it) if it has none (archived or not); in the caller's transaction, which
    holds a lock (for the transaction only) that two chats made at once, by one member or two,
    both take, so they share one."""
    store.execute(select(func.pg_advisory_xact_lock(LOCK)))
    agent = store.scalars(select(Agent).where(Agent.builtin == BUILTIN)).one_or_none()
    if agent is not None:
        return agent
    agent = Agent(
        **built_by(user_id),
        name=free_name(store),
        description=DESCRIPTION,
        builtin=BUILTIN,
    )
    store.add(agent)
    save_agent_version(
        store,
        agent,
        base_prompt=INSTRUCTIONS,
        skills=[],
        tools=[("hybrid_search", validate_tool("hybrid_search", {}))],
        note=None,
        user_id=user_id,
    )
    return agent
