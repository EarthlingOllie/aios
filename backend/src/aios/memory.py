"""Agents' memories: what an agent chose to remember across its runs (`aios.memories`,
`aios.datastores.models.Memory`).

- **Remembering.** The `remember` tool (`aios.tools.memory`) has Jev check the text first:
  flagged text, or text Jev can't check, is refused and nothing is stored. Then `keep` stores it
  with where it was learned (the run, turn and chat). The same text again, ignoring case and
  whitespace (`normalise`), refreshes the memory already there instead of adding one. Over the
  length limit (`memory_max_chars`), or with the agent at its cap (`memory_max_per_agent`), it's
  refused with a message saying so.
- **Recall** is the search tool (`aios.tools.search`): each memory is a search item of kind
  `memory` (`aios.search_index.index_memory`), with the verdict `derived`, which only its own
  agent finds, wherever it works (it's indexed with no run or chat). Like every tool result, it
  reaches the model wrapped as untrusted (`aios.tool_checks`). It doesn't need `remember`: an
  agent version without it still recalls what the agent remembered.
- **Forgetting.** The `forget` tool deletes one of the agent's own memories (`forget`).
- **People** list an agent's memories with where each was learned; editors and admins edit and
  delete them (`aios.api.agents.memories`), each recorded in the audit log.
- **Lifecycle.** A memory belongs to its agent, not its run or chat: archiving the agent keeps
  it, retention never prunes it (pruning a run or chat only clears where it was learned), and
  exports leave it out.
"""

import re
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios import audit
from aios.config import get_settings
from aios.datastores.models import Agent, AgentRun, Invocation, Memory, Turn
from aios.search_index import index_memory


class Refused(ValueError):
    """Why a memory can't be stored, as the agent (or person) is told."""


def normalise(content: str) -> str:
    """`content` as compared for sameness: lower case, whitespace collapsed and trimmed."""
    return re.sub(r"\s+", " ", content).strip().lower()


def too_long(content: str) -> str | None:
    """Why `content` is too long to remember, or None if it isn't."""
    limit = get_settings().memory_max_chars
    if len(content) > limit:
        return (
            f"That's {len(content)} characters: a memory may be at most {limit}. Remember"
            " something shorter."
        )
    return None


def blank(content: str) -> str | None:
    return "There's nothing to remember: the text is blank." if not content.strip() else None


@dataclass(frozen=True)
class Learned:
    """Where a memory was learned: the agent, and its run, turn and chat (any may be unknown)."""

    agent_id: int
    run_id: int | None = None
    turn_id: int | None = None
    chat_id: int | None = None


def learned_in(store: Session, run_id: int) -> Learned | None:
    """Where a memory written now in the run would be learned: its agent, its latest turn, and
    that turn's chat (if a chat started it). None if there's no such run."""
    agent_id = store.scalar(select(AgentRun.agent_id).where(AgentRun.id == run_id))
    if agent_id is None:
        return None
    turn = store.execute(
        select(Turn.id, Invocation.chat_id)
        .outerjoin(Invocation, Invocation.id == Turn.invocation_id)
        .where(Turn.agent_run_id == run_id)
        .order_by(Turn.id.desc())
        .limit(1)
    ).one_or_none()
    return Learned(
        agent_id=agent_id,
        run_id=run_id,
        turn_id=turn.id if turn else None,
        chat_id=turn.chat_id if turn else None,
    )


def _lock_agent(store: Session, agent_id: int) -> None:
    # One memory write per agent at a time, so its cap holds.
    store.execute(select(Agent.id).where(Agent.id == agent_id).with_for_update())


def keep(store: Session, learned: Learned, content: str) -> tuple[Memory, bool]:
    """Remember `content` for the agent, learned where `learned` says, in the caller's
    transaction: the memory, and whether it was one already there (the same text, ignoring case
    and whitespace), refreshed. Raises `Refused` over the length limit, or at the cap. Jev
    must have checked `content` already."""
    content = content.strip()
    if reason := blank(content) or too_long(content):
        raise Refused(reason)
    _lock_agent(store, learned.agent_id)
    normalised = normalise(content)
    existing = store.scalar(
        select(Memory).where(Memory.agent_id == learned.agent_id, Memory.normalised == normalised)
    )
    if existing is None:
        cap = get_settings().memory_max_per_agent
        count = store.scalar(
            select(func.count()).select_from(Memory).where(Memory.agent_id == learned.agent_id)
        )
        if count >= cap:
            raise Refused(
                f"You already have {count} memories, the most you may keep ({cap}). Forget one"
                " you no longer need (search for it to find its memory_id), then remember this."
            )
    values = {
        "agent_id": learned.agent_id,
        "content": content,
        "normalised": normalised,
        "agent_run_id": learned.run_id,
        "turn_id": learned.turn_id,
        "chat_id": learned.chat_id,
    }
    statement = insert(Memory).values(**values)
    memory_id = store.scalar(
        statement.on_conflict_do_update(
            index_elements=["agent_id", "normalised"],
            set_={
                "content": statement.excluded.content,
                "updated_at": func.now(),
                "updated_by": None,
            },
        ).returning(Memory.id)
    )
    memory = store.get(Memory, memory_id, populate_existing=True)
    index_memory(store, memory)
    return memory, existing is not None


def forget(store: Session, agent_id: int, memory_id: int, run_id: int | None) -> bool:
    """Delete one of the agent's own memories (with its search item), recorded in the audit
    log as the agent's. False if it has no memory by that id."""
    content = store.scalar(
        delete(Memory)
        .where(Memory.id == memory_id, Memory.agent_id == agent_id)
        .returning(Memory.content)
    )
    if content is None:
        return False
    audit.record(
        store,
        audit.agent(agent_id, run_id),
        "memory.forgotten",
        "memory",
        memory_id,
        {"agent_id": agent_id},
    )
    return True


def edit(store: Session, memory: Memory, content: str, user_id: int) -> None:
    """A person's edit of a memory, re-indexed and recorded in the audit log, in the caller's
    transaction. Raises `Refused` if it's blank or too long, or the agent already remembers
    the same text in another memory."""
    content = content.strip()
    if reason := blank(content) or too_long(content):
        raise Refused(reason)
    normalised = normalise(content)
    clash = store.scalar(
        select(Memory.id).where(
            Memory.agent_id == memory.agent_id,
            Memory.normalised == normalised,
            Memory.id != memory.id,
        )
    )
    if clash is not None:
        raise Refused("The agent already remembers that, in another memory.")
    before = memory.content
    memory.content = content
    memory.normalised = normalised
    memory.updated_at = func.now()
    memory.updated_by = user_id
    store.flush()
    store.refresh(memory)
    index_memory(store, memory)
    audit.record(
        store,
        audit.person(user_id),
        "memory.edited",
        "memory",
        memory.id,
        {"agent_id": memory.agent_id, "from": before, "to": content},
    )


def remove(store: Session, memory: Memory, user_id: int) -> None:
    """A person's deletion of a memory, recorded in the audit log, in the caller's transaction."""
    audit.record(
        store,
        audit.person(user_id),
        "memory.deleted",
        "memory",
        memory.id,
        {"agent_id": memory.agent_id, "content": memory.content},
    )
    store.delete(memory)
    store.flush()
