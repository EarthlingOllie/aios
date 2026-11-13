"""The default chat agents: the organisation's ordered list of agents that join every new chat
(`aios.assistant.default_chat_agents`, `aios.api.chats.crud.create_chat`), on its Settings tab.

`GET .../organisation/default-chat-agents` is every member's: the list in order, each agent with
whether it's archived (an archived one stays on the list, and is skipped). `PUT` replaces it
(admins only: a 403 for anyone else), given the agents' ids in order: a 422 for an agent the
organisation hasn't got or one named twice. A change is in the audit log, and changes only the
chats made after it.
"""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from aios import audit
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.datastores.models import Agent, DefaultChatAgent
from aios.db import DbId
from aios.permissions import Action, allow

router = APIRouter(prefix="/api")

PATH = "/organisation/default-chat-agents"
# The transaction-level advisory lock changing the list takes, in the organisation's store, so
# two admins' changes at once are one after the other.
LOCK = 0x61696F73_64656661  # "aiosdefa"
# More than anyone would want joining every chat; a bound on the request.
MAX_AGENTS = 20


class DefaultChatAgentOut(BaseModel):
    id: int
    name: str
    archived: bool


class DefaultChatAgentsIn(BaseModel):
    agent_ids: list[DbId] = Field(max_length=MAX_AGENTS)


def listed(store: Session) -> list[DefaultChatAgentOut]:
    rows = store.execute(
        select(Agent.id, Agent.name, Agent.archived_at)
        .join(DefaultChatAgent, DefaultChatAgent.agent_id == Agent.id)
        .order_by(DefaultChatAgent.position)
    ).all()
    return [DefaultChatAgentOut(id=i, name=n, archived=a is not None) for i, n, a in rows]


@router.get(PATH, response_model=list[DefaultChatAgentOut], dependencies=[allow(Action.read)])
def get_default_chat_agents(store: CurrentStore):
    return listed(store)


@router.put(
    PATH,
    response_model=list[DefaultChatAgentOut],
    dependencies=[allow(Action.run_organisation)],
)
def set_default_chat_agents(
    body: DefaultChatAgentsIn, user: CurrentUser, org: CurrentOrganisation, store: CurrentStore
):
    """Replace the list (admins only), in the order given; in the audit log if it changed."""
    ids = body.agent_ids
    if len(set(ids)) != len(ids):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "An agent is named twice.")
    known = set(store.scalars(select(Agent.id).where(Agent.id.in_(ids))))
    if missing := [i for i in ids if i not in known]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown agent: {missing[0]}")
    store.execute(select(func.pg_advisory_xact_lock(LOCK)))
    old = [a.id for a in listed(store)]
    if old != ids:
        store.execute(delete(DefaultChatAgent))
        store.add_all(
            DefaultChatAgent(agent_id=agent_id, position=n, added_by=user.id)
            for n, agent_id in enumerate(ids)
        )
        audit.record(
            store,
            audit.person(user.id),
            "settings.changed",
            "organisation",
            org.id,
            {"setting": "default_chat_agents", "from": old, "to": ids},
        )
        store.commit()
    return listed(store)
