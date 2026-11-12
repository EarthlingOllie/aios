"""`post_message`: an agent posts a markdown message into a chat it's a chat member of, so it can
report where people will see it.

**Refused**, with a reason the model can pass on, and nothing written:

- the chat the turn is answering (its invocation's, or a workflow's it's a node of): that's
  what the reply is for;
- an archived chat, read under the chat's lock, so an archive that gets in first wins;
- a chat the agent isn't a chat member of, or one that doesn't exist;
- past `post_message_posts_per_turn` posts in the turn, counted under the turn's lock, so calls
  made in parallel can't pass it together;
- mentioning anyone or anything that isn't a member of the chat.

**A post** comes from the agent and belongs to the turn's invocation, so it's published to the
chat's stream like any message (`aios.streams`). It's derived text: stored with the verdict
`derived` (the column's default), never checked by Jev, and wrapped as untrusted wherever it
enters model context, as every chat message is (`aios.handoff`, search's tool results). The
people it mentions are notified (`mention`); agents and workflows it mentions start nothing,
since only a person's message starts invocations (`aios.invocations`). Each post is a row in the
usage ledger, a `tool_call` charged to the turn (`aios.usage`).
"""

import json
import re
import uuid
from dataclasses import dataclass
from typing import Annotated

from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from aios import notifications, outbox, search_index, usage
from aios.agent import ChatDeps
from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import (
    Agent,
    Chat,
    ChatMember,
    ChatMessage,
    Invocation,
    Turn,
    Workflow,
)
from aios.db import SessionLocal
from aios.models import ChatAuthorKind, ChatMemberKind, ChatMessageKind, UsageKind, User
from aios.tools.data import in_store, in_thread
from aios.tools.registry import AgentTool, ToolSpec, register

NO_TURN = "Messages can only be posted from a turn of an agent run: nothing was posted."
NO_CHAT = "There's no chat {chat}: nothing was posted."
NOT_A_MEMBER = "You aren't a member of #{name}, so you can't post in it: nothing was posted."
ANSWERING = (
    "#{name} is the chat you're answering: put this in your reply instead. Nothing was posted."
)
ARCHIVED = "#{name} is archived, so nothing can be posted in it: nothing was posted."
CAPPED = (
    "You've posted {most} messages this turn, the most one turn may post: nothing more was posted."
)
NOT_MENTIONABLE = (
    "{names} isn't a member of #{name}, so it can't be mentioned there: nothing was posted."
)
AMBIGUOUS = "More than one member of #{name} is called {mention}: nothing was posted."
# What a chat the agent can't post in is followed by, so it can choose another.
YOURS = " You can post in: {chats}."
MAX_BODY = 100_000


class PostMessageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class _Member:
    kind: ChatMemberKind
    id: int
    name: str


class Refused(Exception):
    """Why nothing was posted, for the model."""


def _token(member: _Member) -> str:
    """A mention as a message writes it (`aios.invocations.TOKEN`)."""
    name = re.sub(r"([\\\[\]])", r"\\\1", member.name)
    return f"@[{name}]({member.kind.value}:{member.id})"


def _find_chat(store: Session, chat: str) -> Chat | None:
    """The chat named `chat` (with or without its `#`; ignoring case if only one chat matches
    that way), or with it as its id."""
    name = chat.strip().removeprefix("#").strip()
    found = store.scalar(select(Chat).where(Chat.name == name))
    if found is None:
        alike = store.scalars(
            select(Chat).where(func.lower(Chat.name) == name.lower()).limit(2)
        ).all()
        found = alike[0] if len(alike) == 1 else None
    if found is None and name.isdigit():
        found = store.get(Chat, int(name))
    return found


def _yours(store: Session, agent_id: int, answering: set[int]) -> str:
    names = store.scalars(
        select(Chat.name)
        .join(ChatMember, ChatMember.chat_id == Chat.id)
        .where(
            ChatMember.agent_id == agent_id,
            Chat.archived_at.is_(None),
            Chat.id.not_in(answering or {0}),
        )
        .order_by(Chat.name)
    ).all()
    return YOURS.format(chats=", ".join(f"#{n}" for n in names)) if names else ""


def _members(store: Session, chat_id: int) -> list[_Member]:
    """The chat's members with their names: people from the platform, agents and workflows
    from the store."""
    rows = store.scalars(select(ChatMember).where(ChatMember.chat_id == chat_id)).all()
    found: list[_Member] = []
    for kind, model, ids in (
        (ChatMemberKind.agent, Agent, [r.agent_id for r in rows if r.agent_id]),
        (ChatMemberKind.workflow, Workflow, [r.workflow_id for r in rows if r.workflow_id]),
    ):
        if ids:
            for i, name in store.execute(select(model.id, model.name).where(model.id.in_(ids))):
                found.append(_Member(kind, i, name))
    people = [r.user_id for r in rows if r.user_id]
    if people:
        with SessionLocal() as db:
            for user in db.scalars(select(User).where(User.id.in_(people))):
                found.append(_Member(ChatMemberKind.user, user.id, user.full_name))
    return found


def _mentioned(store: Session, chat: Chat, names: list[str]) -> list[_Member]:
    """The chat's members named, each once in the order given (`Refused` for a name none of
    them has, or more than one)."""
    if not names:
        return []
    members = _members(store, chat.id)
    found: dict[tuple[ChatMemberKind, int], _Member] = {}
    missing: list[str] = []
    for given in names:
        name = given.strip().removeprefix("@").strip()
        matches = [m for m in members if m.name.lower() == name.lower()]
        if len(matches) > 1:
            raise Refused(AMBIGUOUS.format(name=chat.name, mention=name))
        if not matches:
            missing.append(name)
            continue
        found.setdefault((matches[0].kind, matches[0].id), matches[0])
    if missing:
        raise Refused(NOT_MENTIONABLE.format(names=" or ".join(missing), name=chat.name))
    return list(found.values())


def _with_mentions(body: str, mentioned: list[_Member]) -> str:
    """The body with each mention as its token: in place of its first `@name`, or before the
    rest if the body doesn't name it."""
    leading = []
    for member in mentioned:
        written = re.compile(re.escape("@" + member.name), re.IGNORECASE)
        body, replaced = written.subn(lambda _, m=member: _token(m), body, count=1)
        if not replaced:
            leading.append(_token(member))
    return " ".join([*leading, body]) if leading else body


def _answering(store: Session, invocation: Invocation | None) -> set[int]:
    """The chats the turn is answering: its invocation's, and its ancestors' (a workflow's, for
    a node of it)."""
    if invocation is None:
        return set()
    ids = [invocation.id, *(invocation.ancestor_ids or [])]
    return set(
        store.scalars(
            select(Invocation.chat_id).where(
                Invocation.id.in_(ids), Invocation.chat_id.is_not(None)
            )
        )
    )


def _posts_so_far(store: Session, turn: Turn, agent_id: int, answering: set[int]) -> int:
    """The agent's posts in the turn: its invocation's messages from it, outside the chats it's
    answering, since the turn began."""
    return store.scalar(
        select(func.count())
        .select_from(ChatMessage)
        .where(
            ChatMessage.author_agent_id == agent_id,
            ChatMessage.invocation_id.is_not_distinct_from(turn.invocation_id),
            ChatMessage.kind == ChatMessageKind.text,
            ChatMessage.chat_id.not_in(answering or {0}),
            ChatMessage.created_at >= turn.started_at,
        )
    )


def post(
    store: Session, charge: usage.Charge, chat_name: str, body: str, mentions: list[str]
) -> dict:
    """Post `body` into the chat as the charge's agent, from its turn (in the caller's
    transaction, which it commits); `Refused` with why if it can't."""
    agent_id, turn_id = charge.agent_id, charge.turn_id
    if agent_id is None or turn_id is None:
        raise Refused(NO_TURN)
    # Under the turn's lock: its posts counted, and this one added, one call at a time.
    turn = store.scalar(select(Turn).where(Turn.id == turn_id).with_for_update())
    if turn is None:
        raise Refused(NO_TURN)
    invocation = store.get(Invocation, turn.invocation_id) if turn.invocation_id else None
    answering = _answering(store, invocation)
    chat = _find_chat(store, chat_name)
    is_member = chat is not None and store.scalar(
        select(ChatMember.id).where(ChatMember.chat_id == chat.id, ChatMember.agent_id == agent_id)
    )
    if chat is None:
        raise Refused(NO_CHAT.format(chat=chat_name) + _yours(store, agent_id, answering))
    if chat.id in answering:
        raise Refused(ANSWERING.format(name=chat.name))
    # Archived, read under the chat's lock: an archive that got in first committed, and one
    # after waits for the post.
    archived = store.scalar(
        select(Chat.archived_at).where(Chat.id == chat.id).with_for_update(key_share=True)
    )
    if archived is not None:
        raise Refused(ARCHIVED.format(name=chat.name))
    if not is_member:
        raise Refused(NOT_A_MEMBER.format(name=chat.name) + _yours(store, agent_id, answering))
    most = get_settings().post_message_posts_per_turn
    if _posts_so_far(store, turn, agent_id, answering) >= most:
        raise Refused(CAPPED.format(most=most))
    mentioned = _mentioned(store, chat, mentions)
    message = ChatMessage(
        chat_id=chat.id,
        author_kind=ChatAuthorKind.agent,
        author_agent_id=agent_id,
        kind=ChatMessageKind.text,
        body=_with_mentions(body, mentioned),
        mentions=[{"kind": m.kind.value, "id": m.id, "name": m.name} for m in mentioned],
        invocation_id=turn.invocation_id,
    )
    store.add(message)
    search_index.index_chat_message(store, message)
    outbox.message_posted(store, message, invocation_id=turn.invocation_id)
    # Posting is activity.
    store.execute(update(Chat).where(Chat.id == chat.id).values(updated_at=func.now()))
    usage.record(
        store, charge, UsageKind.tool_call, cost=0.0, call_key=f"post_message:{message.id}"
    )
    # Only the people mentioned are told; agents and workflows mentioned start nothing.
    people = [m.id for m in mentioned if m.kind == ChatMemberKind.user]
    if people:
        notifications.after_store_commit(
            store, people, notifications.MENTION, notifications.chat_ref(chat.id, message.id)
        )
    store.commit()
    return {"posted": {"chat": chat.name, "message_id": message.id}}


def _post(
    data_store_id: uuid.UUID, charge: usage.Charge, chat: str, body: str, mentions: list[str]
) -> dict:
    with store_session(data_store_id, "rw") as store:
        try:
            return post(store, charge, chat, body, mentions)
        except Refused as exc:
            store.rollback()
            return {"error": str(exc)}


async def _post_in(
    data_store_id: uuid.UUID, charge: usage.Charge, chat: str, body: str, mentions: list[str]
) -> dict:
    return await in_thread(data_store_id, _post, charge, chat, body, mentions)


DESCRIPTION = """Post a markdown message into another chat you're a member of, to report where \
people will see it. Name the chat (as #name or just the name). Not the chat you're answering, \
whose reply is your answer, nor an archived chat; and only so many posts a turn.

To mention members of that chat, list their names in `mentions`: each is written in place of \
its first `@name` in the body (or before it). People mentioned are notified; agents and \
workflows mentioned aren't started."""


def build(config: PostMessageConfig) -> list[AgentTool]:
    async def post_message(
        chat: Annotated[str, "The chat to post in: its name (with or without #)."],
        body: Annotated[str, Field(min_length=1, max_length=MAX_BODY), "The message, markdown."],
        runtime: ToolRuntime[ChatDeps],
        mentions: Annotated[
            list[str] | None, "Names of the chat's members to mention (people, agents, workflows)."
        ] = None,
    ) -> str:
        charge = usage.current()
        if runtime.context.run_id is None or charge is None:
            return json.dumps({"error": NO_TURN})
        if not body.strip():
            return json.dumps({"error": "The message is empty: nothing was posted."})
        return await in_store(runtime, _post_in, charge, chat, body, list(mentions or []))

    return [StructuredTool.from_function(coroutine=post_message, description=DESCRIPTION)]


register(
    ToolSpec(
        key="post_message",
        name="Post message",
        description="Post into other chats the agent is a member of, to report where people"
        " will see it. People mentioned are notified; agents and workflows mentioned start"
        " nothing.",
        config_model=PostMessageConfig,
        build=build,
    )
)
