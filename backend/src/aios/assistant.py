"""The documents assistant: a built-in agent that answers from the documents, and joins each new
chat (`aios.api.chats.create_chat`).

It's an ordinary agent of the organisation's, one per organisation, made by the app for
whichever member first needs it (who is its `created_by`), with the document search tool
(`hybrid_search`) and instructions to ground its answers in what it finds. Being an agent,
mentioning it in a chat runs a turn of it like any agent member (with the acknowledgement,
brief, card and posted reply, `aios.invocations`). Any member can change it as they would any
of the organisation's agents; `builtin` is how it's found again, whatever it's been renamed.
Deleted, it's made afresh for the next chat.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from aios.api.provenance import made_by
from aios.models import Agent, Organisation
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


def free_name(db: Session, org: Organisation) -> str:
    """`NAME`, or the first of "NAME 2", "NAME 3"… none of the organisation's live agents is
    called."""
    taken = set(
        db.scalars(
            select(Agent.name).where(
                Agent.organisation_id == org.id,
                Agent.deleted_at.is_(None),
                Agent.name.startswith(NAME),
            )
        )
    )
    n = 1
    while (name := NAME if n == 1 else f"{NAME} {n}") in taken:
        n += 1
    return name


def documents_assistant(db: Session, org: Organisation, user_id: int) -> Agent:
    """The organisation's documents assistant, made by `user_id` (the member who needs it) if it
    has none (live); in the caller's transaction, which it holds the organisation's row locked
    in, so two chats made at once, by one member or two, share one."""
    db.execute(select(Organisation.id).where(Organisation.id == org.id).with_for_update())
    agent = db.scalars(
        select(Agent).where(
            Agent.organisation_id == org.id, Agent.builtin == BUILTIN, Agent.deleted_at.is_(None)
        )
    ).one_or_none()
    if agent is not None:
        return agent
    agent = Agent(
        **made_by(org.id, user_id),
        name=free_name(db, org),
        description=DESCRIPTION,
        builtin=BUILTIN,
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
