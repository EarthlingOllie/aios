# Chats

**Chats** are group conversations, and belong to the organisation. `/` opens the Chats section:
it lands on the most recently active chat you're in, or the new-chat form. A chat's members are
**chat members**: people, agents and workflows. Mentioning an agent or workflow starts a run of
it (an **invocation**), briefed on the chat first.

## Chats and chat members

A chat of another organisation doesn't exist for you (a 404 on every endpoint), and names are
unique within the organisation (a 409), like channels, so two organisations can each have
"general". The **Chats** page lists the organisation's chats you're in, and its public ones you
aren't (marked "Not joined", so you can find them), most recently active first, with a search
box over their names and descriptions and filter buttons (All, Joined, Not joined, Unread,
Private), kept in the URL as on the Agents list, and as the list refreshes (it has no sort
menu). It opens one to its header (name, description, chat member count, Edit, Delete, Join or
Leave, and which member created it: `created_by`, `updated_by`, as `aios.api.provenance`
reports them) above two tabs, as on the agent page: **Chat**, the messages and composer (the
default), and **Members** (`/chats/5/members`, with the count). The room isn't kept while
Members is open, so nothing is marked read there and coming back reloads the messages, scrolled
to the newest; a message typed but not sent is still there on coming back, from Members, the
chat's Edit page, another section or a reload (kept in the tab's session storage, per user and
chat).

Chat members are grouped by kind: users, **agents and workflows**, with the icons the
navigation uses. The owner adds any member of the organisation, and any of its agents and
workflows, whoever made them (`POST /api/chats/{id}/members` with `{kind, id}`; a user from
outside it is a 404, as one who doesn't exist is; one row per chat and user, agent or workflow;
a workflow since deleted is no longer a chat member; an archived agent still is, shown as
archived, but isn't offered to mention, and a mention of it fails at once: see
[Agents and skills](agents-and-skills.md#archiving-an-agent)).

A chat is **public** or **private**: a private chat doesn't exist for anyone who isn't a chat
member, while a public one can be opened by every member of the organisation. Whoever creates a
chat owns it and is its first chat member (a role on their chat member row, not ownership of
data; one owner per chat); only the owner renames or describes it, changes its visibility, adds
or removes chat members and deletes it (a 403 for other chat members). A public chat you aren't
in can be read but not posted in until you **join** it, in one click
(`POST /api/chats/{id}/join`); any chat member but the owner can **leave**
(`POST /api/chats/{id}/leave`), and the chat leaves their list (a private one is then a 404 for
them). Leaving or being removed deletes nothing: whoever joins or is added later reads the
history from the start. When a member leaves the organisation, see
[Accounts and organisations](accounts-and-organisations.md#leaving) for what happens to the
chats they owned.

Each new chat has the organisation's documents assistant as its first agent (see
[Agents and skills](agents-and-skills.md#the-documents-assistant)).

## Unread counts

Each chat you're in shows an **unread count** in the list: the messages others posted since the
last one you saw, tracked per chat and chat member by message id
(`chat_members.last_read_message_id`). Reading the open chat to the bottom, with its tab
visible, reports the newest message (`POST /api/chats/{id}/read` with `{message_id}`; it only
moves forward) and clears the count; the list asks again every 5 seconds (30 while hidden).
Joining counts what was already there as read; a chat member added by the owner has the history
to read. Nothing is sent off-app.

## Messages

Chat members post **messages** in markdown, shown with their author's name (a user's full name,
"First Last") and initial (an agent's or workflow's icon). The open chat asks for what's new
every second (`GET /api/chats/{id}/messages?after=N`, every 30 seconds while its tab is hidden,
and at once when it's shown again), follows new messages when scrolled to the bottom and
otherwise leaves the reader where they are; scrolling to the top loads the page before
(`?before=N`, 50 at a time). Anyone a chat exists for can read it; only its chat members post (a
403 for anyone else). A read returns `{messages, invocations}` (below). Posting makes a chat the
most recently active; a message stays when its author is deleted, with no author, or removed
from the chat.

## Mentions

Typing `@` in the composer offers the chat's chat members (`GET /api/chats/{id}/mentionable`),
grouped by kind so an agent and a workflow with the same name are told apart, and narrows them
as you type: a user by their first or last name (`@ali` and `@smi` both find Alice Smith), an
agent or workflow by its name, ignoring case. Picking one inserts a token, `@[name](kind:id)`,
which points at the chat member by id (so it still does once they're renamed), and the message
is posted with its **mentions** (`{body, mentions: [{kind, id}]}`), stored structurally as
`[{kind, id, name}]`; the message shows each as a chip with the chat member's kind and name, and
a message that mentions you is highlighted. Mentioning anyone who isn't a chat member is a 422
(and a token typed by hand for one is plain text).

### Mentioning a workflow

Mentioning a **workflow** starts a run of it (`aios.invocations`, `chat_invocations`). The post
creates, in the message's own transaction, a queued invocation per workflow mentioned and the
workflow's acknowledgement with a **run card** under it, and returns them with the message
(`{messages, invocations}`), so the chat never shows a mention without its card. Just after the
response, the run starts through the same helpers starting one by hand and schedules use (and
the firing machinery, `aios.firing`: see [Triggers](triggers.md#firing)), with the message as
its input (each mention as `@name`), started by the member who asked (the run's `created_by`);
the invocation records who asked, the run, and goes `running`.

A start that's refused (no nodes, an agent that can't run, the queue down) fails the invocation
with the reason. When the run ends (in the worker, found stopped, or deleted before it
finished), its output or error is posted back as a message from the workflow, belonging to the
invocation, and the invocation goes `succeeded` or `failed`, in one transaction. One whose start
was lost with its process fails when the chat's next read finds it still queued (or briefing)
after 90 seconds. A read's `invocations` are the state of each invocation its messages belong to
and of every one in the chat still going, each with its run as the run's page shows it (graph,
steps), so a chat's live cards cost its one poll a second. The card draws the run's graph still,
at a fixed height, with each node's state, "n of m nodes done" and a status badge, and opens the
run's page when clicked; a run that couldn't start shows why, and one since deleted says so.
Mentions in what a workflow posts start nothing, so a chat can't loop. Runs started from a chat
are listed with the workflow's others, marked with the chat (while it exists); deleting a chat
deletes its messages and invocations, not the runs.

### Mentioning an agent

Mentioning an **agent** runs one turn of a new run of its current version, with the message as
the user's, started by the member who asked; its acknowledgement and card come with the post in
the same way. The turn runs in an API process through the firing machinery schedules use, under
the same cap (`SCHEDULER_MAX_CONCURRENT_RUNS`): a process with a free slot claims it, just after
the post or on a scheduler tick (until then it waits, queued, however long), and keeps its
heartbeat while it runs. A turn whose heartbeat stops (its process killed, say, by a deploy)
fails its card with "The service stopped mid-run." when the chat is next read or a tick sweeps,
and a turn in flight as a process shuts down records the same. A process with
`SCHEDULER_ENABLED=false` runs no chat turns either. A read's `invocations` carry each agent
run's status, start and finish times, tool calls and tokens so far; the card shows a status
badge, the time elapsed (ticking in the browser from the start time), the tool calls and tokens,
and opens the run's page when clicked. When the turn ends, the agent's final reply is posted
back as a message from the agent (or the turn's error, failing the card). Several agents and
workflows mentioned in one message each start; mentions in an agent's reply start nothing. Runs
started from a chat are listed with the agent's others, marked with the chat.

## The handoff brief

Before a mention's run starts, a **handoff** step (`aios.handoff`) briefs it on the chat, so the
chat member doesn't have to be told the context again. It runs for agents and workflows alike,
and is never skipped: the invocation goes `queued` → `briefing` → `running`, and one structured
call to the handoff model reads the chat up to the mentioning message (its last
`HANDOFF_WINDOW_MESSAGES` messages, 30 by default, or about `HANDOFF_WINDOW_TOKENS`, 8000, of
them, whichever is less; run cards aside) and returns `{ack, brief}`. The ack, a line saying
what the chat member understood it's being asked to do, replaces the acknowledgement's
placeholder ("Reading the chat…"), so whoever asked can correct it early; a read's invocations
carry it (`acknowledgement`), so a card already on screen updates. The **brief** is kept on the
invocation (`brief`) and the run's input is the brief, a blank line, then the message (just the
message when the chat had nothing to brief it on). The card shows the brief behind a
disclosure, so anyone can see exactly what the run was told. A handoff that fails (the model
errs, takes over a minute, or answers without `{ack, brief}`) fails the invocation with why, as
a refused start does. The model is `HANDOFF_MODEL`, by default the deployment's `LLM_MODEL`;
each call is traced to Langfuse tagged `handoff`, with the invocation's id and its chat as the
session (see [Tracing](tracing.md)).
