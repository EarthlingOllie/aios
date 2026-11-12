"""Chats: group conversations between users, agents and workflows.

A chat belongs to an organisation (`aios.organisations`), and doesn't exist for anyone outside it:
every endpoint is a 404 for them. Within it, every chat is open: anyone in the organisation lists
it (`is_member` says whether they've joined), and reads its messages, members and run cards
without joining. A chat has a name unique among the organisation's chats (a 409), a description,
and says which member made it and last changed it (`aios.api.provenance`). Its members are
people, agents and workflows of the organisation (an archived agent or workflow stays one, shown
as archived). Chats have no owners, and what anyone may do to one is the chat rules'
(`aios.api.chats.rules`), which each chat lists for its reader (`actions`). People join in one
step (or by posting) and leave as easily, the last one too: the chat stays. Nobody adds or
removes a person. Its members (not others) rename and describe it, and add or remove its agents
and workflows: a 403 for anyone who hasn't joined. Admins archive and unarchive it: an archived
chat is read-only (a 409 for posts, joins, edits and adds), listed apart (`?archived=true`), and
still read and searched. Renames, archiving and unarchiving are in the audit log. Nobody deletes
a chat. Someone leaving the organisation leaves its chats, with nothing handed over. Leaving
deletes nothing, and whoever joins later reads the history from the start. A new chat's first
agent is the organisation's documents assistant (`aios.assistant`).

The agents and workflows among a chat's members are the ones it includes: only those can be
mentioned there. A message mentioning one it doesn't include is a 422 naming what's missing
(`detail.missing`), so the screen can offer to add it and send the message.

Every message a person posts goes through one path (`post_message`): the permission and mention
checks, then Jev (`aios.jev`) with the source "chat message", before anything is written. A
message Jev flags is a 422 with its reason; one Jev can't check in time
(`jev_chat_timeout_seconds`) is a 503 saying to try again; either way nothing is stored, indexed
or started. One that passes is stored `clean`, and its search item says so; posting joins the
poster in the same transaction. What agents and workflows post is `derived`, never checked here.

Each user member keeps the last message they've seen (`POST /api/chats/{id}/read`, as they read
to the bottom), and the list counts, per chat they're in, the messages others posted since
(`unread_count`).

People post markdown messages, read by id: `?after=N` for what's new (what an open chat reads
while its stream can't open), `?before=N` for the page of history before N, and neither for the
latest page. A chat's last activity is when it was last changed or a message was last posted.

Everything here is live: what changes in a chat (messages, run cards, its header and members) is
published once committed (`aios.streams`), and an open chat follows it on its stream, the chat
list on the person's (`aios.api.streams`). Nothing polls.

A message can mention members: the composer picks them from `GET /api/chats/{id}/mentionable`, the
body carries a token for each (`@[name](kind:id)`) and the message stores them structurally,
`[{kind, id, name}]`, so an agent and a workflow with the same name are never confused. Mentioning
a person who isn't a member is a 422, as is an agent or workflow the chat doesn't include.

Mentioning a workflow starts a run of it, with the message as its input (`aios.invocations`): the
response to the post carries the workflow's acknowledgement and run card with the message, and
its start is enqueued on the run service (`enqueue_starts`, `aios.run_service`), which starts
the run. Before it does, the handoff (`aios.handoff`) briefs it on the chat: the invocation is
`briefing`, then its `brief` is kept and prepended to the run's input, and its ack replaces the
acknowledgement's placeholder (`acknowledgement`, which its `invocation` event carries, so a
card already shown follows it). A read's `invocations` are the state of each invocation its
messages belong to and of every one in the chat still going, each with its run as the run's page
shows it; the chat's stream sends each as it changes. When the run ends, its result is posted
back as a message from the workflow.

Mentioning an agent runs one turn of a new run of it, with the brief and the message as the
user's, the same way: its acknowledgement and card come with the post, and the run service runs
the handoff and then the turn, in the interactive lane, retrying it elsewhere if its process
dies, and failing its card after a few attempts. A read's `invocations` carry an agent run's
status, start and counters (`agent_run`), and the agent's final reply is posted back when the
turn ends. While it's being written, the chat's stream carries it as a draft (never stored),
which the posted reply replaces.

Either run is started by the member who mentioned it (its `created_by`) and executes for the
organisation, over its store, like any other run: it doesn't depend on whoever made the agent or
workflow, and every member of the organisation reads it, in the chat or not.

The API is split by sub-domain (`crud`, `members`, `messages`, `invocations`, and the chat rules
in `rules`) and mounted as one router. Every name is re-exported here, where callers import it;
`newest_message_id`, `enqueue_starts` and `MESSAGE_PAGE` are read here at call time, so a test
that replaces one here replaces it for every part.
"""

from fastapi import APIRouter

from aios.api.chats import crud as _crud
from aios.api.chats import members as _members
from aios.api.chats import messages as _messages
from aios.api.chats.common import (
    CHAT_NAME_INDEX,
    LOAD,
    MEMBER_CONSTRAINTS,
    TARGET_MODELS,
    ChatCreateIn,
    ChatIn,
    ChatMemberOut,
    ChatMembersOut,
    ChatOut,
    ChatSummary,
    MemberIn,
    chat_out,
    chat_summary,
    get_visible_chat,
    is_member,
    load,
    lock_chat,
    member_key,
    member_out,
    members_out,
    ordered_members,
    still_member,
    user_member,
)
from aios.api.chats.crud import (
    ChatListed,
    archive_chat,
    create_chat,
    get_chat,
    list_chats,
    unarchive_chat,
    update_chat,
)
from aios.api.chats.invocations import enqueue_starts
from aios.api.chats.members import (
    MentionableOut,
    MentionablesOut,
    add_member,
    join,
    join_chat,
    leave_chat,
    list_mentionable,
    mentionable_out,
    mentionables,
    newest_message_id,
    remove_member,
    take_out,
)
from aios.api.chats.messages import (
    FLAGGED,
    MESSAGE_PAGE,
    NOT_A_MEMBER_READING,
    NOT_INCLUDED,
    NOT_MENTIONABLE,
    UNCHECKED,
    AgentRunLiveOut,
    InvocationOut,
    MentionIn,
    MentionOut,
    MessageAuthorOut,
    MessageIn,
    MessageOut,
    MessagesOut,
    ReadIn,
    agent_runs_live,
    author_out,
    invocation_out,
    list_messages,
    mark_read,
    message_out,
    messages_out,
    post_message,
    resolve_mentions,
    with_runs,
)
from aios.api.chats.rules import ALREADY_A_MEMBER, ARCHIVED, NOT_JOINED, ChatAction

# One router for the package, its sub-domains' routes in the order they were declared.
router = APIRouter()
router.include_router(_crud.router)
router.include_router(_members.router)
router.include_router(_messages.router)

__all__ = [
    "ALREADY_A_MEMBER",
    "ARCHIVED",
    "ChatAction",
    "AgentRunLiveOut",
    "CHAT_NAME_INDEX",
    "ChatCreateIn",
    "ChatIn",
    "ChatListed",
    "ChatMemberOut",
    "ChatMembersOut",
    "ChatOut",
    "ChatSummary",
    "InvocationOut",
    "LOAD",
    "MEMBER_CONSTRAINTS",
    "FLAGGED",
    "MESSAGE_PAGE",
    "MemberIn",
    "MentionIn",
    "MentionOut",
    "MentionableOut",
    "MentionablesOut",
    "MessageAuthorOut",
    "MessageIn",
    "MessageOut",
    "MessagesOut",
    "NOT_A_MEMBER_READING",
    "NOT_INCLUDED",
    "NOT_MENTIONABLE",
    "UNCHECKED",
    "NOT_JOINED",
    "ReadIn",
    "TARGET_MODELS",
    "add_member",
    "agent_runs_live",
    "author_out",
    "chat_out",
    "chat_summary",
    "archive_chat",
    "create_chat",
    "enqueue_starts",
    "get_chat",
    "get_visible_chat",
    "invocation_out",
    "is_member",
    "join",
    "join_chat",
    "leave_chat",
    "list_chats",
    "list_mentionable",
    "list_messages",
    "ordered_members",
    "load",
    "lock_chat",
    "mark_read",
    "member_key",
    "member_out",
    "members_out",
    "mentionable_out",
    "mentionables",
    "message_out",
    "messages_out",
    "newest_message_id",
    "post_message",
    "remove_member",
    "resolve_mentions",
    "still_member",
    "take_out",
    "unarchive_chat",
    "update_chat",
    "user_member",
    "with_runs",
]
