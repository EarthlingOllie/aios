# Chats

**Chats** are open conversations, and belong to the organisation: anyone in it reads any chat. `/` opens the Chats section:
it lands on the most recently active chat you're in, or the new-chat form. A chat's members are
**chat members**: people, agents and workflows. Mentioning an agent or workflow starts a run of
it (an **invocation**), briefed on the chat first.

## Chats and chat members

A chat of another organisation doesn't exist for you (a 404 on every endpoint), and names are
unique within the organisation (a 409), like channels, so two organisations can each have
"general". The **Chats** page lists every chat of the organisation, those you haven't joined
marked "Not joined", most recently active first, with a search
box over their names and descriptions and filter buttons (All, Joined, Not joined, Unread,
Archived), kept in the URL as on the Agents list, and as the list refreshes (it has no sort
menu). Archived chats are only under **Archived** (`GET /api/o/{org}/chats?archived=true`). It
opens one to its header (name, description, chat member count, the actions you may take: Edit,
Archive or Unarchive, Join or Leave; and which member created it: `created_by`, `updated_by`, as `aios.api.provenance`
reports them) above two tabs, as on the agent page: **Chat**, the messages and composer (the
default), and **Members** (`/chats/5/members`, with the count). The room isn't kept while
Members is open, so nothing is marked read there and coming back reloads the messages, scrolled
to the newest; a message typed but not sent is still there on coming back, from Members, the
chat's Edit page, another section or a reload (kept in the tab's session storage, per user and
chat).

Chat members are grouped by kind: users, **agents and workflows**, with the icons the
navigation uses. Any chat member adds any of the organisation's agents and workflows, whoever
made them (`POST /api/o/{org}/chats/{id}/members` with `{kind, id}`; one row per chat and agent or
workflow; an archived agent or workflow still is, shown as archived, but isn't offered to
mention, and a mention of it fails at once: see
[Agents and skills](agents-and-skills.md#archiving-an-agent)), and removes them
(`DELETE /api/o/{org}/chats/{id}/members/{member_id}`). People join themselves: nobody adds a
person to a chat or removes one, admins included (a 403).

Every chat is **open**: there's no visibility and no owner, and every chat member is equal.
Anyone in the organisation reads any chat (its messages, chat members and run cards) without
joining it, **joins** it in one click (`POST /api/o/{org}/chats/{id}/join`), or posts in it, which
joins them in the same transaction. Whoever creates a chat is its first chat member. Its chat
members (anyone who has joined) rename or describe it and add or remove its agents and
workflows (a 403, saying to join first, for anyone who hasn't). Anyone can **leave**
(`POST /api/o/{org}/chats/{id}/leave`), the last person too: the chat stays, to read and join
again. Leaving deletes nothing: whoever joins later reads the history from the start. Leaving the
organisation takes a person out of its chats and hands nothing over (see
[Accounts and organisations](accounts-and-organisations.md#leaving)). Nobody deletes a chat.

These are **the chat rules** (`aios.api.chats.rules`), one check the API enforces and the
screens follow: each chat says which actions you may take on it (`actions`: `post`, `join`,
`leave`, `edit`, `include`, `archive`, `unarchive`), and the page shows those and hides the rest.

## Archiving

**Admins** archive and unarchive chats (`POST /api/o/{org}/chats/{id}/archive`, `.../unarchive`;
a 403 for editors and participants): their only extra power over chats. An archived chat says
when and by whom (`archived_at`, `archived_by`) and is **read-only**, with a banner saying so and
no composer: posts (and so mentions), joins, edits, and adding or removing its agents and
workflows are a 409 ("This chat is archived, so it's read-only"), checked before anything else a
post does, Jev's check included. It's still read, its chat members still leave it, and its
messages are still found by search. Runs already under way in it when it's archived finish and
post their results there. Renames (`chat.renamed`), archiving (`chat.archived`) and unarchiving
(`chat.unarchived`) are in the [audit log](stores.md#the-audit-log).

Each new chat has the organisation's documents assistant as its first agent (see
[Agents and skills](agents-and-skills.md#the-documents-assistant)).

## Chat with this agent

Talking to one agent happens in an open chat like any other. **Chat with this agent**, on an
agent's page, creates a chat with that agent and no name (`POST /api/o/{org}/chats` with
`{agent_id}`): a new chat that includes only that agent (not the documents assistant), with the
person as its first chat member, named "<Agent> – <person> – <date>" (the date in UTC, as
`2026-11-12`). A clash with any chat's name, archived ones too, adds " (2)", " (3)" and so on, so
each click makes a new chat. It needs permission to invoke the agent: someone its **invocable by**
leaves out is refused with the reason (a 403, "Only editors and admins can invoke the agent
Payroll."), and an archived agent is a 409. The chat opens with a mention of the agent in the
composer; its chat members rename it like any chat. Every chat page says that the whole
organisation can see it.

## Unread counts

Each chat you're in shows an **unread count** in the list: the messages others posted since the
last one you saw, tracked per chat and chat member by message id
(`chat_members.last_read_message_id`). Reading the open chat to the bottom, with its tab
visible, reports the newest message (`POST /api/o/{org}/chats/{id}/read` with `{message_id}`; it only
moves forward) and clears the count; the list follows the person's stream (below), so counts
go up as others post.
Joining (or posting, which joins) counts what was already there as read. Nothing is sent off-app.

## Messages

Chat members post **messages** in markdown, shown with their author's name (a user's full name,
"First Last") and initial (an agent's or workflow's icon). The open chat follows its stream
(below) for what's new, follows new messages when scrolled to the bottom and
otherwise leaves the reader where they are; scrolling to the top loads the page before
(`?before=N`, 50 at a time). Anyone in the organisation reads it and posts in it (posting joins:
a note under the composer says so to anyone who hasn't joined). A read returns `{messages, invocations}` (below). Posting makes a chat the
most recently active; a message stays when its author is deleted, with no author, or removed
from the chat.

## Mentions

Typing `@` in the composer offers the chat's chat members (`GET /api/o/{org}/chats/{id}/mentionable`),
grouped by kind so an agent and a workflow with the same name are told apart, and narrows them
as you type: a user by their first or last name (`@ali` and `@smi` both find Alice Smith), an
agent or workflow by its name, ignoring case. Picking one inserts a token, `@[name](kind:id)`,
which points at the chat member by id (so it still does once they're renamed), and the message
is posted with its **mentions** (`{body, mentions: [{kind, id}]}`), stored structurally as
`[{kind, id, name}]`; the message shows each as a chip with the chat member's kind and name, and
a message that mentions you is highlighted. Mentioning a person who isn't a chat member is a 422
(and a token typed by hand for one is plain text).

The agents and workflows among a chat's chat members are the ones it **includes**, and only
those can be mentioned there. The composer offers the organisation's other agents and workflows
after the chat's own; a message mentioning one the chat doesn't include is refused before
anything is stored, a 422 naming it (`detail: {message, missing: [{kind, id, name}]}`), and the
message stays in the composer under **Add it to this chat**, which joins you if need be, adds
it, and sends the message.

### The Jev check

Every message a person posts goes through one path, and is checked by Jev (`aios.jev`, the
prompt-injection classifier) after the permission and mention checks and before anything is
written, with the source "chat message":

- a message Jev **flags** is refused, a 422 with Jev's reason (`detail: {message, reason}`), and
  nothing is written: no message, chat membership, invocation or search item;
- if Jev errors or doesn't answer within `JEV_CHAT_TIMEOUT_SECONDS` (3 by default), it's a 503,
  "Couldn't check this message, try again", and nothing is written;
- a message that passes is stored with the verdict `clean` (`chat_messages.verdict`), and so is
  its search item. What agents and workflows post isn't checked here, and is `derived`.

The room keeps a refused message in the composer, with why. While Jev's health check reports it
down (`GET /api/o/{org}/jev-status`), the composer says messages can't be checked right now.

Whatever Jev said, people's messages reach models inside an untrusted block (`aios.jev.wrap`):
the mentioning message and the chat the handoff reads, and the message a run is given; so do
briefs, which are derived text.

### Mentioning a workflow

Mentioning a **workflow** starts a run of it (`aios.invocations`, the store's `invocations`,
with mention as their cause). The post
creates, in the message's own transaction, a queued invocation per workflow mentioned and the
workflow's acknowledgement with a **run card** under it, and returns them with the message
(`{messages, invocations}`), so the chat never shows a mention without its card. Just after the
response, its start is enqueued on the [run service](run-service.md) in the interactive lane,
which runs the handoff and then starts the run through the same helpers starting one by hand
and schedules use, with the message as its input (each mention as `@name`), started by the member who asked (the run's `created_by`);
the invocation records who asked, the run, and goes `running`.

A start that's refused (no nodes, an agent that can't run, the queue down) fails the invocation
with the reason. When the run ends (in the worker, found stopped, or deleted before it
finished), its output or error is posted back as a message from the workflow, belonging to the
invocation, and the invocation goes `succeeded` or `failed`, in one transaction. One whose enqueue was lost is put
back by the run service's requeue sweep after a minute; one whose start keeps losing its process
fails after three attempts with "The service stopped mid-run." A read's `invocations` are the state of each invocation its messages belong to
and of every one in the chat still going, each with its run as the run's page shows it (graph,
steps), and the chat's stream sends each as it changes. The card draws the run's graph still,
at a fixed height, with each node's state, "n of m nodes done" and a status badge, and opens the
run's page when clicked; a run that couldn't start shows why, and one since deleted says so.
Mentions in what a workflow posts start nothing, so a chat can't loop. Runs started from a chat
are listed with the workflow's others, marked with the chat (while it exists); deleting a chat
deletes its messages and invocations, not the runs.

### Mentioning an agent

Mentioning an **agent** runs one turn of a new run of its current version, with the message as
the user's, started by the member who asked; its acknowledgement and card come with the post in
the same way. The turn runs on the [run service](run-service.md), in the interactive lane: it
waits, queued, until a process claims it (people waiting in chat come first, and another
organisation's burst doesn't hold it up), then the handoff runs, then the turn. A turn whose
process dies (killed, say, by a deploy) is retried on another, carrying on from the run's last
checkpoint; after three attempts its card fails with "The service stopped mid-run." A read's `invocations` carry each agent
run's status, start and finish times, tool calls and tokens so far; the card shows a status
badge, the time elapsed (ticking in the browser from the start time), the tool calls and tokens,
and opens the run's page when clicked. When the turn ends, the agent's final reply is posted
back as a message from the agent (or the turn's error, failing the card). Several agents and
workflows mentioned in one message each start; mentions in an agent's reply start nothing. Runs
started from a chat are listed with the agent's others, marked with the chat.

### Agents posting into other chats

An agent given the **Post message** tool (`post_message`, `aios.tools.post_message`) posts
markdown into other chats it's a chat member of, by name, to report where people will see it.
It's refused, with a reason the agent is told and nothing written, for the chat its turn is
answering (its reply is for that), an archived chat, a chat it isn't in (naming those it is),
past `POST_MESSAGE_POSTS_PER_TURN` (20) posts in one turn, and a mention of anyone or anything
that isn't in the chat. A post is from the agent, belongs to the turn's invocation and arrives
on the chat's stream like any message. It's derived text: stored `derived`, never checked by
Jev, and wrapped as untrusted wherever it enters model context. The people it mentions are
notified (`mention`); the agents and workflows it mentions start nothing. Each post is a tool
call in the [usage ledger](usage-and-limits.md#the-usage-ledger).

## Live updates

Nothing polls. Conversations and the chat list update live over server-sent events, fed through
Redis pub/sub: the API and the run service publish what they change once it's committed
(`aios.streams`), and each API process relays it to the streams it holds (`aios.api.streams`).

- **A chat's stream** (`GET /api/o/{org}/chats/{id}/stream`): anyone in the organisation opens
  any chat's, joined or not (a 404 for anyone else). It carries `message` (as the messages read
  returns it, its id the event id), `invocation` (a run card's state, as it goes queued, briefing,
  running and finished), `draft` (the whole reply so far of an agent's turn, marked "writing…",
  which the posted reply replaces in place; a failed turn's draft goes, and its failure shows),
  `chat` (its header: an archived chat turns read-only, still open), `members`, `gone` and
  `ping`.
- **A person's stream** (`GET /api/me/stream`), across every organisation they're in: `chat_activity`
  (a chat's newest message and their unread count) and `chat_list` (a chat appeared, was joined,
  left, renamed, archived or unarchived), and `ping`.
- **Reconnecting.** A stream resumes after the last event id (`Last-Event-ID`, or
  `?last_event_id=`): it subscribes first, then reads the messages after it, the chat's
  unfinished invocations and any drafts, and sends them before what it buffered, each message
  once. The browser always resumes after the newest message it shows.
- **Drafts** are kept in Redis while the turn runs, with an expiry (`DRAFT_EXPIRY_SECONDS`, 15
  minutes), and published whole at most every `DRAFT_PUBLISH_INTERVAL_SECONDS` (150 ms). They
  never reach the database.
- **Closing.** Leaving the organisation closes the person's streams of it with `gone` and the
  reason, and the browser stops reconnecting. Leaving a chat doesn't: anyone can read it.
- **Limits.** An API process holds at most `STREAMS_PER_PROCESS` streams (2,000): past it a new
  one is a 503 with a retry hint (`Retry-After`, `STREAM_RETRY_SECONDS`). A person holds at most
  `STREAMS_PER_PERSON` (20) in a process: past it, their oldest closes with `gone: replaced`.
- **Fallback.** While Redis is down, streams answer 503 (and open ones end); the browser reads
  what's new every 5 seconds and opens the stream again by itself once it can.
- **Heartbeats** (`ping`) go every `STREAM_HEARTBEAT_SECONDS` (15). The proxies don't buffer
  `/api` (`proxy_buffering off` in the compose and full-stack nginx configs) and time out well
  above the heartbeat (`proxy_read_timeout 300s`, the load balancer's `idle_timeout` 300).

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
message when the chat had nothing to brief it on), each in an untrusted block. The card shows the brief behind a
disclosure, so anyone can see exactly what the run was told. A handoff that fails (the model
errs, takes over a minute, or answers without `{ack, brief}`) fails the invocation with why, as
a refused start does. The model is `HANDOFF_MODEL`, by default the deployment's `LLM_MODEL`;
each call is traced to Langfuse tagged `handoff`, with the invocation's id and its chat as the
session (see [Tracing](tracing.md)).
