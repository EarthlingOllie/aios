"""The audit log, for admins (`aios.audit`): filtered by actor, action and date, newest first,
a page at a time."""

from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from aios import audit
from aios.api.provenance import UserOut, people
from aios.auth import CurrentOrganisation, CurrentStore
from aios.datastores.models import Agent
from aios.db import DB
from aios.models import AuditActorKind
from aios.permissions import Action, allow

router = APIRouter(prefix="/api")


class AgentRef(BaseModel):
    id: int
    name: str | None


class ActorOut(BaseModel):
    kind: AuditActorKind
    # A person's, or the person who started an agent's run.
    user: UserOut | None
    agent: AgentRef | None
    run_id: int | None


class AuditEntryOut(BaseModel):
    id: int
    at: datetime
    actor: ActorOut
    action: str
    target_kind: str | None
    target_id: str | None
    details: dict[str, Any]


class AuditPageOut(BaseModel):
    entries: list[AuditEntryOut]
    # The cursor of the next (older) page, as `before`; null on the last.
    next: str | None


@router.get("/audit-log", response_model=AuditPageOut, dependencies=[allow(Action.audit)])
def read_audit_log(
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
    actor: Annotated[int | None, Query(description="A person, by user id.")] = None,
    agent: Annotated[int | None, Query(description="An agent, by id.")] = None,
    action: Annotated[
        str | None, Query(max_length=200, description="An action, or its family (`agent`).")
    ] = None,
    since: Annotated[date | None, Query(description="On or after this day (UTC).")] = None,
    until: Annotated[date | None, Query(description="On or before this day (UTC).")] = None,
    before: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    filters = audit.Filters(
        actor_user_id=actor, actor_agent_id=agent, action=action, since=since, until=until
    )
    try:
        found, cursor = audit.entries(store, filters, before=before, limit=limit)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That isn't a cursor") from None
    known = people(db, org.id, [e.actor_user_id for e in found])
    agent_ids = {e.actor_agent_id for e in found if e.actor_agent_id is not None}
    names = (
        dict(store.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids))).all())
        if agent_ids
        else {}
    )
    return AuditPageOut(
        entries=[
            AuditEntryOut(
                id=e.id,
                at=e.at,
                actor=ActorOut(
                    kind=e.actor_kind,
                    user=known.get(e.actor_user_id),
                    agent=(
                        AgentRef(id=e.actor_agent_id, name=names.get(e.actor_agent_id))
                        if e.actor_agent_id is not None
                        else None
                    ),
                    run_id=e.actor_run_id,
                ),
                action=e.action,
                target_kind=e.target_kind,
                target_id=e.target_id,
                details=e.details,
            )
            for e in found
        ],
        next=cursor,
    )
