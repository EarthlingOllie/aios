"""A chat's members: who can be mentioned, adding and removing its agents and workflows, and
people joining and leaving. Nobody adds or removes a person (`aios.api.chats.rules`)."""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios import organisations

# Read on the package at call time, where tests replace them.
from aios.api import chats as package
from aios.api.chats import rules
from aios.api.chats.common import (
    MEMBER_CONSTRAINTS,
    TARGET_MODELS,
    ChatOut,
    MemberIn,
    Targets,
    Users,
    chat_out,
    chat_targets,
    chat_users,
    get_visible_chat,
    load,
    lock_chat,
    member_key,
    member_out,
    ordered_members,
    still_member,
    user_member,
)
from aios.api.chats.rules import ALREADY_A_MEMBER, ChatAction
from aios.api.skills import lock_live, on_conflict
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.datastores.models import Chat, ChatMember, ChatMessage
from aios.db import DB, DbId
from aios.models import ChatMemberKind
from aios.permissions import Action, CurrentRole, allow

router = APIRouter(prefix="/api")


class MentionableOut(BaseModel):
    """A member as the composer offers it to mention, and as a message's `mentions` name it."""

    kind: ChatMemberKind
    # The user's, agent's or workflow's id.
    id: int
    # A user's full name ("First Last").
    name: str
    # A user's names, which the composer matches what's typed after "@" against; null for an
    # agent or a workflow (matched on `name`).
    first_name: str | None
    last_name: str | None


class MentionablesOut(BaseModel):
    """Who can be mentioned in a chat: its members, grouped by kind, as `ChatMembersOut` orders
    them. An agent and a workflow can share a name; their kinds tell them apart."""

    users: list[MentionableOut]
    agents: list[MentionableOut]
    workflows: list[MentionableOut]


def mentionable_out(member: ChatMember, targets: Targets, users: Users) -> MentionableOut | None:
    out = member_out(member, targets, users)
    if out is None:
        return None
    user = users.get(member.user_id) if member.kind == ChatMemberKind.user else None
    return MentionableOut(
        kind=out.kind,
        id=out.target_id,
        name=out.name,
        first_name=user.first_name if user else None,
        last_name=user.last_name if user else None,
    )


def mentionables(
    db: Session, store: Session, chat: Chat, *, archived: bool = False
) -> MentionablesOut:
    """The chat's members to offer for mentioning: not its archived agents and workflows, unless
    `archived` (a mention of one is still taken, and fails at once: `aios.invocations.create`)."""
    targets = chat_targets(store, chat)
    users = chat_users(db, chat)
    by_kind: dict[ChatMemberKind, list[MentionableOut]] = {kind: [] for kind in ChatMemberKind}
    for member in ordered_members(chat):
        target = targets.get((member.kind, member.agent_id or member.workflow_id))
        if not archived and target is not None and target[1]:
            continue
        if (out := mentionable_out(member, targets, users)) is not None:
            by_kind[member.kind].append(out)
    return MentionablesOut(
        users=by_kind[ChatMemberKind.user],
        agents=by_kind[ChatMemberKind.agent],
        workflows=by_kind[ChatMemberKind.workflow],
    )


@router.get(
    "/chats/{chat_id}/mentionable",
    response_model=MentionablesOut,
    dependencies=[allow(Action.read)],
)
def list_mentionable(chat_id: DbId, user: CurrentUser, db: DB, store: CurrentStore):
    """The chat's members, for the composer's `@` autocomplete."""
    return mentionables(db, store, get_visible_chat(store, user.id, chat_id))


@router.post(
    "/chats/{chat_id}/members",
    response_model=ChatOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[allow(Action.post)],
)
def add_member(
    chat_id: DbId,
    body: MemberIn,
    user: CurrentUser,
    org: CurrentOrganisation,
    role: CurrentRole,
    db: DB,
    store: CurrentStore,
):
    """Add one of the organisation's agents or workflows to the chat (any of its members; not an
    archived chat). Nobody adds a person, who joins themselves: a 403 for everyone, admins
    included."""
    chat = get_visible_chat(store, user.id, chat_id)
    if body.kind == ChatMemberKind.user:
        raise HTTPException(status.HTTP_403_FORBIDDEN, rules.NO_ADDING_PEOPLE)
    rules.check(role, chat, user.id, ChatAction.include)
    model = TARGET_MODELS[body.kind]
    # One in the organisation's store, not an archived one. Share-locked there until the
    # store's transaction ends, after the add commits, so a concurrent archive either waits
    # for the add or wins it.
    target = store.get(model, body.id)
    if target is None or not lock_live(store, model, [target.id], read=True):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown {body.kind}")
    member = ChatMember(kind=body.kind, added_by=user.id, **{f"{body.kind}_id": target.id})
    # After the agent or workflow is locked, the order everything that locks both takes, so the
    # two can't wait on each other.
    still_member(store, chat, user.id)
    if member_key(member) in {member_key(m) for m in chat.members}:
        raise HTTPException(status.HTTP_409_CONFLICT, ALREADY_A_MEMBER)
    chat.members.append(member)
    # Adding a member is activity too.
    chat.updated_at = func.now()
    # The check above can lose a race with another add of the same member; this catches that.
    with on_conflict(store, MEMBER_CONSTRAINTS[body.kind], ALREADY_A_MEMBER):
        store.commit()
    return chat_out(db, store, org.id, load(store, chat.id), user.id, role)


def newest_message_id(store: Session, chat_id: int) -> int | None:
    """The id of the chat's newest message; None while it has none."""
    return store.scalar(select(func.max(ChatMessage.id)).where(ChatMessage.chat_id == chat_id))


@router.post("/chats/{chat_id}/join", response_model=ChatOut, dependencies=[allow(Action.post)])
def join_chat(
    chat_id: DbId,
    user: CurrentUser,
    org: CurrentOrganisation,
    role: CurrentRole,
    db: DB,
    store: CurrentStore,
):
    """Join a chat of the organisation's, not an archived one (posting in it joins too: `join`).
    What was posted before they joined counts as seen: they could read it, and it's all there to
    scroll. A 404 for a user who left the organisation as they joined
    (`organisations.holds_member`)."""
    chat = get_visible_chat(store, user.id, chat_id)
    rules.check(role, chat, user.id, ChatAction.join)
    # Before the chat's lock, as leaving takes the organisation's before the chats'.
    if not organisations.holds_member(db, org, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    join(store, chat, user.id)
    # Joining is activity, as adding a member is.
    chat.updated_at = func.now()
    store.commit()
    db.commit()
    return chat_out(db, store, org.id, load(store, chat.id), user.id, role)


def join(store: Session, chat: Chat, user_id: int) -> None:
    """Add the user to the chat (`get_visible_chat`'s), under its lock (`lock_chat`: a 409 if
    it's been archived), with what was posted so far counting as seen: for joining, and for
    posting, which joins. Nothing if they've joined meanwhile (two posts at once, say). In the
    caller's transaction."""
    lock_chat(store, chat, writing=True)
    store.execute(
        insert(ChatMember)
        .values(
            chat_id=chat.id,
            kind=ChatMemberKind.user,
            user_id=user_id,
            added_by=user_id,
            last_read_message_id=package.newest_message_id(store, chat.id),
        )
        .on_conflict_do_nothing(constraint=MEMBER_CONSTRAINTS[ChatMemberKind.user])
    )


def take_out(store: Session, member: ChatMember) -> None:
    """Delete the member's row, for a person leaving the chat or an agent or workflow being
    removed from it. A statement,
    not the ORM's delete: their leaving the organisation deletes it too, and one that got in after
    it was read is waited for here and leaves nothing to delete. They're out either way, which is
    what was asked, where the ORM would warn that it found no row (or raise, were rows
    versioned)."""
    store.execute(delete(ChatMember).where(ChatMember.id == member.id))


@router.post(
    "/chats/{chat_id}/leave",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[allow(Action.post)],
)
def leave_chat(chat_id: DbId, user: CurrentUser, role: CurrentRole, store: CurrentStore) -> None:
    """Leave a chat the user is a member of: anyone can, the last person too, and the chat
    stays, archived or not. Nothing they posted goes with them, and they can still read it."""
    chat = get_visible_chat(store, user.id, chat_id)
    rules.check(role, chat, user.id, ChatAction.leave)
    member = user_member(chat, user.id)
    assert member is not None
    lock_chat(store, chat)
    take_out(store, member)
    # As removing a member is.
    chat.updated_at = func.now()
    store.commit()


@router.delete(
    "/chats/{chat_id}/members/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[allow(Action.post)],
)
def remove_member(
    chat_id: DbId, member_id: DbId, user: CurrentUser, role: CurrentRole, store: CurrentStore
) -> None:
    """Remove an agent or workflow from the chat (any of its members; not an archived chat).
    Nobody removes a person, who leaves themselves: a 403 for everyone, admins included."""
    chat = get_visible_chat(store, user.id, chat_id)
    member = next((m for m in chat.members if m.id == member_id), None)
    if member is not None and member.kind == ChatMemberKind.user:
        raise HTTPException(status.HTTP_403_FORBIDDEN, rules.NO_REMOVING_PEOPLE)
    rules.check(role, chat, user.id, ChatAction.include)
    still_member(store, chat, user.id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    take_out(store, member)
    chat.updated_at = func.now()
    store.commit()
