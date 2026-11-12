"""The chat rules: the one check of what a person may do to a chat, which the routes enforce and
the screens follow (each chat says which of these its reader may take: `actions`).

Chats have no owners, and every chat member is equal:

- Anyone in the organisation reads any chat, joins it, and posts in it (which joins them).
- Any chat member leaves, renames it, describes it, and adds or removes its agents and
  workflows.
- Admins archive and unarchive it: their only extra power over chats.
- Nobody adds another person to a chat or removes one (people join and leave themselves), and
  nobody deletes a chat.

An archived chat is read-only: posts (and so mentions), joins, edits and adds are a 409 saying
so, checked before anything else a post does (Jev's check included). It's still read and
searched, its members still leave it, and the runs already under way in it still post.
"""

import enum

from fastapi import HTTPException, status

from aios.datastores.models import Chat
from aios.models import ChatMemberKind, Role
from aios.permissions import TABLE, Action

ARCHIVED = "This chat is archived, so it's read-only. An admin can unarchive it."
ALREADY_A_MEMBER = "Already a member of this chat."
NOT_JOINED = "Only the chat's members can do that: join it first."
NOT_A_MEMBER = "Not a member of this chat."
NO_ADDING_PEOPLE = "Nobody adds a person to a chat: people join it themselves."
NO_REMOVING_PEOPLE = "Nobody removes a person from a chat: people leave it themselves."


class ChatAction(enum.StrEnum):
    # Post a message, mentions and all (joining, if they haven't).
    post = "post"
    join = "join"
    leave = "leave"
    # Rename it or change its description.
    edit = "edit"
    # Add or remove its agents and workflows.
    include = "include"
    archive = "archive"
    unarchive = "unarchive"


# What an archived chat refuses (read-only).
WRITES = frozenset({ChatAction.post, ChatAction.join, ChatAction.edit, ChatAction.include})
# What only its members do.
MEMBERS = frozenset({ChatAction.leave, ChatAction.edit, ChatAction.include})
# What only admins do.
ADMINS = frozenset({ChatAction.archive, ChatAction.unarchive})


def refusal(
    role: Role, action: ChatAction, *, joined: bool, archived: bool
) -> HTTPException | None:
    """Why a member with `role`, who has `joined` the chat or not, may not take `action` on it,
    `archived` or not: None if they may."""
    if action in ADMINS:
        if role not in TABLE[Action.archive_chats]:
            return HTTPException(status.HTTP_403_FORBIDDEN, f"Only admins can {action} chats.")
        return None
    if archived and action in WRITES:
        return HTTPException(status.HTTP_409_CONFLICT, ARCHIVED)
    if action == ChatAction.join and joined:
        return HTTPException(status.HTTP_409_CONFLICT, ALREADY_A_MEMBER)
    if action == ChatAction.leave and not joined:
        return HTTPException(status.HTTP_409_CONFLICT, NOT_A_MEMBER)
    if action in MEMBERS and not joined:
        return HTTPException(status.HTTP_403_FORBIDDEN, NOT_JOINED)
    return None


def allowed(role: Role, *, joined: bool, archived: bool) -> list[ChatAction]:
    """The actions a member with `role` may take on a chat they've `joined` or not, `archived` or
    not, as the screens show them: archiving only while it isn't, unarchiving only while it is."""
    return [
        action
        for action in ChatAction
        if refusal(role, action, joined=joined, archived=archived) is None
        and not (action == ChatAction.archive and archived)
        and not (action == ChatAction.unarchive and not archived)
    ]


def check(role: Role, chat: Chat, user_id: int, action: ChatAction) -> None:
    """Refuse (a 403 or 409 saying why) unless the user, with `role`, may take `action` on the
    chat as it was read (its members loaded)."""
    joined = any(m.kind == ChatMemberKind.user and m.user_id == user_id for m in chat.members)
    refused = refusal(role, action, joined=joined, archived=chat.archived_at is not None)
    if refused is not None:
        raise refused
