"""The documents assistant: a built-in agent that answers from its owner's documents, and joins
each new chat they make (`aios.api.chats.create_chat`).

It's an ordinary agent of its owner's, made for them by the app the first time they need it,
with the document search tool (`hybrid_search`) and instructions to ground its answers in what
it finds. Being an agent, mentioning it in a chat runs a turn of it like any agent member (with
the acknowledgement, brief, card and posted reply, `aios.invocations`), as its owner: over the
documents of the user who owns the chat. Its owner can change it as they would any agent of
theirs; `builtin` is how it's found again, whatever it's been renamed. Deleted, it's made afresh
for the next chat.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from aios.models import Agent, User
from aios.tools import validate_tool
from aios.versioning import save_agent_version

BUILTIN = "documents"
NAME = "Documents assistant"
DESCRIPTION = "Answers from your documents. Built in: it joins each new chat you make."
INSTRUCTIONS = (
    "You are a helpful assistant with access to the user's uploaded documents. "
    "When a question may be answered by those documents, call `search_documents` first "
    "and ground your answer in the results, citing sources as [filename]. "
    "If the documents don't contain the answer, say so plainly."
)


def free_name(db: Session, user_id: int) -> str:
    """`NAME`, or the first of "NAME 2", "NAME 3"… none of the user's live agents is called."""
    taken = set(
        db.scalars(
            select(Agent.name).where(
                Agent.owner_id == user_id,
                Agent.deleted_at.is_(None),
                Agent.name.startswith(NAME),
            )
        )
    )
    n = 1
    while (name := NAME if n == 1 else f"{NAME} {n}") in taken:
        n += 1
    return name


def documents_assistant(db: Session, user_id: int) -> Agent:
    """The user's documents assistant, made for them if they have none (live); in the caller's
    transaction, which it holds the user's row locked in, so two chats made at once share one."""
    db.execute(select(User.id).where(User.id == user_id).with_for_update())
    agent = db.scalars(
        select(Agent).where(
            Agent.owner_id == user_id, Agent.builtin == BUILTIN, Agent.deleted_at.is_(None)
        )
    ).one_or_none()
    if agent is not None:
        return agent
    agent = Agent(
        owner_id=user_id, name=free_name(db, user_id), description=DESCRIPTION, builtin=BUILTIN
    )
    db.add(agent)
    save_agent_version(
        db,
        agent,
        base_prompt=INSTRUCTIONS,
        skills=[],
        tools=[("hybrid_search", validate_tool("hybrid_search", {}))],
        note=None,
        user_id=user_id,
    )
    return agent
