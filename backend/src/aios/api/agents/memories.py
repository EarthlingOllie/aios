"""An agent's memories (`aios.memory`), as its Memory tab shows them: every member sees them, with
where each was learned; editors and admins edit and delete them, each recorded in the audit log.
Archived agents' memories too: archiving keeps them."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from aios import memory
from aios.api.agents.common import get_org_agent
from aios.api.provenance import UserOut, people
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.datastores.models import AgentRun, Chat, Memory
from aios.db import DB, DbId
from aios.jev import Verdict
from aios.permissions import Action, allow

router = APIRouter(prefix="/api")


class MemoryOut(BaseModel):
    id: int
    content: str
    # Always `derived`: what an agent wrote, recalled wrapped as untrusted.
    verdict: Verdict = Verdict.derived
    # Where it was learned: the run and turn that wrote it, and the chat that run was in (each
    # null if it wasn't in one, or it has since been pruned or deleted).
    agent_run_id: int | None
    run_title: str | None
    turn_id: int | None
    chat_id: int | None
    chat_name: str | None
    created_at: datetime
    updated_at: datetime
    # The member who last edited it; null if the agent wrote it as it is.
    updated_by: UserOut | None


class MemoryIn(BaseModel):
    content: str = Field(min_length=1)


def _rows(store, agent_id: int, memory_id: int | None = None):
    query = (
        select(Memory, AgentRun.title, Chat.name)
        .outerjoin(AgentRun, AgentRun.id == Memory.agent_run_id)
        .outerjoin(Chat, Chat.id == Memory.chat_id)
        .where(Memory.agent_id == agent_id)
        .order_by(Memory.updated_at.desc(), Memory.id.desc())
    )
    if memory_id is not None:
        query = query.where(Memory.id == memory_id)
    return store.execute(query).all()


def _out(db, org_id: int, rows) -> list[MemoryOut]:
    known = people(db, org_id, [m.updated_by for m, *_ in rows])
    return [
        MemoryOut(
            id=m.id,
            content=m.content,
            agent_run_id=m.agent_run_id,
            run_title=title,
            turn_id=m.turn_id,
            chat_id=m.chat_id,
            chat_name=chat_name,
            created_at=m.created_at,
            updated_at=m.updated_at,
            updated_by=known.get(m.updated_by),
        )
        for m, title, chat_name in rows
    ]


def _memory(store, agent_id: int, memory_id: int) -> Memory:
    found = store.scalar(
        select(Memory).where(Memory.id == memory_id, Memory.agent_id == agent_id).with_for_update()
    )
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return found


@router.get(
    "/agents/{agent_id}/memories",
    response_model=list[MemoryOut],
    dependencies=[allow(Action.read)],
)
def list_memories(agent_id: DbId, org: CurrentOrganisation, db: DB, store: CurrentStore):
    """The agent's memories, the latest remembered or edited first, with where each was
    learned."""
    agent = get_org_agent(store, agent_id)
    return _out(db, org.id, _rows(store, agent.id))


@router.patch(
    "/agents/{agent_id}/memories/{memory_id}",
    response_model=MemoryOut,
    dependencies=[allow(Action.build)],
)
def edit_memory(
    agent_id: DbId,
    memory_id: DbId,
    body: MemoryIn,
    user: CurrentUser,
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
):
    """Change what the agent remembers. 422 if it's too long; 409 if the agent already
    remembers the same text in another memory."""
    agent = get_org_agent(store, agent_id)
    found = _memory(store, agent.id, memory_id)
    if reason := memory.blank(body.content) or memory.too_long(body.content.strip()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, reason)
    try:
        memory.edit(store, found, body.content, user.id)
    except memory.Refused as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    store.commit()
    [out] = _out(db, org.id, _rows(store, agent.id, found.id))
    return out


@router.delete(
    "/agents/{agent_id}/memories/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[allow(Action.build)],
)
def delete_memory(agent_id: DbId, memory_id: DbId, user: CurrentUser, store: CurrentStore):
    """Delete the memory: the agent no longer recalls it."""
    agent = get_org_agent(store, agent_id)
    memory.remove(store, _memory(store, agent.id, memory_id), user.id)
    store.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
