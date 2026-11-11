"""Chats: group conversations between users, agents and workflows.

A chat belongs to an organisation (`aios.organisations`), and doesn't exist for anyone outside it:
every endpoint is a 404 for them. Within it, a chat has an owner (the member who made it: a role in
the chat, held on their member row, not ownership of data), a name unique among the organisation's
chats, a description and a visibility, and says which member made it and last changed it
(`aios.api.provenance`). Its members are users, agents and workflows of the organisation (an agent
or workflow since deleted is no longer one; its row stays, and what it posted stays either way). A
private chat doesn't exist for a member who isn't in it either. A public one exists for every
member of the organisation: the list carries the public chats a member isn't in (`is_member` false)
for them to find, and they join it in one step and leave it as easily (the owner can't: a chat
always has one). Only the owner can change a chat, add or remove its members or delete it: a 403
for anyone else. An owner who leaves the organisation hands the chat to its member added first
(`organisations.leave`); with nobody left in it, whoever joins a public one next owns it, and a
private one, which nobody could join, is deleted, as its owner deleting it would be. Leaving the
chat or being removed from it deletes nothing, and whoever joins later reads the history from the
start. A new chat's first agent is the organisation's documents assistant (`aios.assistant`).

Each user member keeps the last message they've seen (`POST /api/chats/{id}/read`, as they read
to the bottom), and the list counts, per chat, the messages others posted since (`unread_count`).

Its members post markdown messages, read by id: `?after=N` for what's new (what the open chat
polls), `?before=N` for the page of history before N, and neither for the latest page. Anyone
the chat exists for can read them; only a member can post (a 403 for anyone else). A chat's last
activity is when it was last changed or a message was last posted.

A message can mention members: the composer picks them from `GET /api/chats/{id}/mentionable`, the
body carries a token for each (`@[name](kind:id)`) and the message stores them structurally,
`[{kind, id, name}]`, so an agent and a workflow with the same name are never confused. Mentioning
anyone who isn't a member is a 422.

Mentioning a workflow starts a run of it, with the message as its input (`aios.invocations`): the
response to the post carries the workflow's acknowledgement and run card with the message, and
the run starts just after (`InvocationFiring`). Before it does, the handoff (`aios.handoff`)
briefs it on the chat: the invocation is `briefing`, then its `brief` is kept and prepended to the
run's input, and its ack replaces the acknowledgement's placeholder (`acknowledgement`, which the
poll carries, so a card already shown follows it). A read's `invocations` are the state of each
invocation its messages belong to and of every one in the chat still going, each with its run
as the run's page shows it, so a chat's live cards cost its one poll. When the run ends, its
result is posted back as a message from the workflow.

Mentioning an agent runs one turn of a new run of it, with the brief and the message as the
user's, the same way: its acknowledgement and card come with the post, and the turn runs in this
process through the firing machinery schedules use (`TurnFiring`, `aios.firing`), under its cap:
it's claimed when there's a free slot (`launch_turns`, just after the post or on a scheduler
tick), beaten while it runs, and failed if its heartbeat stops (`fail_stopped_turns`), so a turn
lost with its process fails its card. A read's `invocations` carry an agent run's status, start
and counters (`agent_run`), and the agent's final reply is posted back when the turn ends.

Either run is started by the member who mentioned it (its `created_by`) and executes for the
organisation, over its store, like any other run: it doesn't depend on whoever made the agent or
workflow, and every member of the organisation reads it, in the chat or not.

The API is split by sub-domain (`crud`, `members`, `messages`, `invocations`) and mounted as one
router. Every name is re-exported here, where callers import it; `newest_message_id`,
`start_invocations` and `MESSAGE_PAGE` are read here at call time, so a test that replaces one
here replaces it for every part.
"""

from fastapi import APIRouter

from aios.api.chats import crud as _crud
from aios.api.chats import members as _members
from aios.api.chats import messages as _messages
from aios.api.chats.common import (
    ALREADY_A_MEMBER,
    CHAT_NAME_INDEX,
    LOAD,
    MEMBER_CONSTRAINTS,
    OWNER_ONLY,
    TARGET_MODELS,
    ChatIn,
    ChatMemberOut,
    ChatMembersOut,
    ChatOut,
    ChatSummary,
    MemberIn,
    chat_out,
    chat_summary,
    get_chat_if_owner,
    get_visible_chat,
    is_member,
    is_owner,
    load,
    lock_chat,
    lock_owner,
    member_key,
    member_out,
    members_out,
    ordered_members,
    still_owner,
    user_member,
)
from aios.api.chats.crud import (
    ChatListed,
    create_chat,
    delete_chat,
    get_chat,
    list_chats,
    update_chat,
)
from aios.api.chats.invocations import (
    InvocationFiring,
    TurnFiring,
    fail_stopped_turns,
    launch_turns,
    run_turns,
    start_invocations,
)
from aios.api.chats.members import (
    MentionableOut,
    MentionablesOut,
    add_member,
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
    MESSAGE_PAGE,
    NOT_A_MEMBER,
    NOT_A_MEMBER_READING,
    NOT_MENTIONABLE,
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
    catch_up,
    invocation_out,
    list_messages,
    mark_read,
    message_out,
    messages_out,
    post_message,
    resolve_mentions,
    with_runs,
)

# One router for the package, its sub-domains' routes in the order they were declared.
router = APIRouter()
router.include_router(_crud.router)
router.include_router(_members.router)
router.include_router(_messages.router)

__all__ = [
    "ALREADY_A_MEMBER",
    "AgentRunLiveOut",
    "CHAT_NAME_INDEX",
    "ChatIn",
    "ChatListed",
    "ChatMemberOut",
    "ChatMembersOut",
    "ChatOut",
    "ChatSummary",
    "InvocationFiring",
    "InvocationOut",
    "LOAD",
    "MEMBER_CONSTRAINTS",
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
    "NOT_A_MEMBER",
    "NOT_A_MEMBER_READING",
    "NOT_MENTIONABLE",
    "OWNER_ONLY",
    "ReadIn",
    "TARGET_MODELS",
    "TurnFiring",
    "add_member",
    "agent_runs_live",
    "author_out",
    "catch_up",
    "chat_out",
    "chat_summary",
    "create_chat",
    "delete_chat",
    "fail_stopped_turns",
    "get_chat",
    "get_chat_if_owner",
    "get_visible_chat",
    "invocation_out",
    "is_member",
    "is_owner",
    "join_chat",
    "launch_turns",
    "leave_chat",
    "list_chats",
    "list_mentionable",
    "list_messages",
    "ordered_members",
    "load",
    "lock_chat",
    "lock_owner",
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
    "run_turns",
    "start_invocations",
    "still_owner",
    "take_out",
    "update_chat",
    "user_member",
    "with_runs",
]
