"""A chat's members: who can be mentioned, adding and removing them, joining and leaving."""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from aios import organisations

# Read on the package at call time, where tests replace them.
from aios.api import chats as package
from aios.api.chats.common import (
    ALREADY_A_MEMBER,
    MEMBER_CONSTRAINTS,
    TARGET_MODELS,
    ChatOut,
    MemberIn,
    chat_out,
    get_chat_if_owner,
    get_visible_chat,
    is_member,
    load,
    lock_owner,
    member_key,
    member_out,
    ordered_members,
    still_owner,
    user_member,
)
from aios.api.skills import lock_live, on_conflict
from aios.auth import CurrentOrganisation, CurrentUser
from aios.db import DB, DbId
from aios.models import (
    Chat,
    ChatMember,
    ChatMemberKind,
    ChatMemberRole,
    ChatMessage,
)

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


def mentionable_out(member: ChatMember) -> MentionableOut:
    out = member_out(member)
    user = member.user if member.kind == ChatMemberKind.user else None
    return MentionableOut(
        kind=out.kind,
        id=out.target_id,
        name=out.name,
        first_name=user.first_name if user else None,
        last_name=user.last_name if user else None,
    )


def mentionables(chat: Chat, *, archived: bool = False) -> MentionablesOut:
    """The chat's members to offer for mentioning: not its archived agents and workflows, unless
    `archived` (a mention of one is still taken, and fails at once: `aios.invocations.create`)."""
    by_kind: dict[ChatMemberKind, list[MentionableOut]] = {kind: [] for kind in ChatMemberKind}
    for member in ordered_members(chat):
        target = member.agent or member.workflow
        if not archived and target is not None and target.archived_at is not None:
            continue
        by_kind[member.kind].append(mentionable_out(member))
    return MentionablesOut(
        users=by_kind[ChatMemberKind.user],
        agents=by_kind[ChatMemberKind.agent],
        workflows=by_kind[ChatMemberKind.workflow],
    )


@router.get("/chats/{chat_id}/mentionable", response_model=MentionablesOut)
def list_mentionable(chat_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB):
    """The chat's members, for the composer's `@` autocomplete."""
    return mentionables(get_visible_chat(db, org, user.id, chat_id))


@router.post(
    "/chats/{chat_id}/members", response_model=ChatOut, status_code=status.HTTP_201_CREATED
)
def add_member(chat_id: DbId, body: MemberIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
    """Add a member of the organisation, or one of its agents or workflows, to the chat (its
    owner only). A user outside the organisation doesn't exist here: a 404, as one who doesn't
    exist at all is, and as one who leaves it as they're added is (`organisations.holds_member`:
    they'd be left in a chat of an organisation they're not in)."""
    chat = get_chat_if_owner(db, org, user.id, chat_id)
    if body.kind == ChatMemberKind.user:
        if not organisations.holds_member(db, org, body.id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown user")
        member = ChatMember(
            kind=body.kind, user_id=body.id, role=ChatMemberRole.member, added_by=user.id
        )
    else:
        model = TARGET_MODELS[body.kind]
        target = db.get(model, body.id)
        # Not an archived one. Locked until commit, so a concurrent archive either waits for the
        # add or wins it.
        if (
            target is None
            or target.organisation_id != org.id
            or not lock_live(db, model, [target.id], read=True)
        ):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown {body.kind}")
        member = ChatMember(kind=body.kind, added_by=user.id, **{f"{body.kind}_id": target.id})
    # After the agent or workflow is locked, the order everything that locks both takes, so the
    # two can't wait on each other.
    still_owner(db, chat, user.id)
    if member_key(member) in {member_key(m) for m in chat.members}:
        raise HTTPException(status.HTTP_409_CONFLICT, ALREADY_A_MEMBER)
    chat.members.append(member)
    # Adding a member is activity too.
    chat.updated_at = func.now()
    # The check above can lose a race with another add of the same member; this catches that.
    with on_conflict(db, MEMBER_CONSTRAINTS[body.kind], ALREADY_A_MEMBER):
        db.commit()
    return chat_out(db, load(db, chat.id), user.id)


def newest_message_id(db: Session, chat_id: int) -> int | None:
    """The id of the chat's newest message; None while it has none."""
    return db.scalar(select(func.max(ChatMessage.id)).where(ChatMessage.chat_id == chat_id))


@router.post("/chats/{chat_id}/join", response_model=ChatOut)
def join_chat(chat_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB):
    """Join a public chat as a member (a private one exists only for its members), or as its
    owner if it has none (its owner left the organisation with nobody in it to hand it to:
    `organisations.leave`, which deletes such a chat if it was private). What was posted
    before they joined counts as seen: they could read it, and it's all there to scroll. A 404
    for a user who left the organisation as they joined (`organisations.holds_member`)."""
    chat = get_visible_chat(db, org, user.id, chat_id)
    if is_member(chat, user.id):
        raise HTTPException(status.HTTP_409_CONFLICT, ALREADY_A_MEMBER)
    # Before the chat's lock, as leaving takes the organisation's before the chats'.
    if not organisations.holds_member(db, org, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    # Under the chat's lock, so that two joining an ownerless chat at once don't both become its
    # owner (the second sees the first), and one joining as its owner leaves the organisation
    # doesn't join as a member of a chat that has just lost its owner (`lock_owner`).
    owner = lock_owner(db, chat)
    chat.members.append(
        ChatMember(
            kind=ChatMemberKind.user,
            user_id=user.id,
            role=ChatMemberRole.member if owner is not None else ChatMemberRole.owner,
            added_by=user.id,
            last_read_message_id=package.newest_message_id(db, chat.id),
        )
    )
    # Joining is activity, as adding a member is.
    chat.updated_at = func.now()
    # Two joins at once: the second loses here.
    with on_conflict(db, MEMBER_CONSTRAINTS[ChatMemberKind.user], ALREADY_A_MEMBER):
        db.commit()
    return chat_out(db, load(db, chat.id), user.id)


def take_out(db: Session, member: ChatMember) -> None:
    """Delete the member's row, for their leaving the chat or being removed from it. A statement,
    not the ORM's delete: their leaving the organisation deletes it too, and one that got in after
    it was read is waited for here and leaves nothing to delete. They're out either way, which is
    what was asked, where the ORM would warn that it found no row (or raise, were rows
    versioned)."""
    db.execute(delete(ChatMember).where(ChatMember.id == member.id))


@router.post("/chats/{chat_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
def leave_chat(chat_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB) -> None:
    """Leave a chat the user is a member of but doesn't own (a chat always has its owner).
    Nothing they posted goes with them; a private chat stops existing for them."""
    chat = get_visible_chat(db, org, user.id, chat_id)
    member = user_member(chat, user.id)
    if member is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Not a member of this chat.")
    # Their role read again under the chat's lock: the chat may have just been handed to them, its
    # owner having left the organisation (`lock_owner`).
    if lock_owner(db, chat) == user.id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "The owner can't leave the chat."
        )
    take_out(db, member)
    # As removing a member is.
    chat.updated_at = func.now()
    db.commit()


@router.delete("/chats/{chat_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    chat_id: DbId, member_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB
) -> None:
    """Remove a member from the chat (its owner only). The owner stays: a chat always has one."""
    chat = get_chat_if_owner(db, org, user.id, chat_id)
    still_owner(db, chat, user.id)
    member = next((m for m in chat.members if m.id == member_id), None)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if member.role == ChatMemberRole.owner:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "The owner can't be removed from the chat."
        )
    take_out(db, member)
    chat.updated_at = func.now()
    db.commit()
