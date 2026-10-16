# Channels: Slack-style group chat with agents

Status: **plan** (2026-10-16). Nothing built yet. The open decisions at the end need answers
before Phase 1's schema is fixed.

## Goal

Replace today's one-to-one chat with **channels**: named group conversations (`#ops`,
`#hiring`), each holding the user, any number of agents and, later, other users.

- Anyone in a channel can **@mention** anyone else in it. A user @mentioning an agent asks it to
  act. An agent @mentioning a user asks them for something (an answer, an approval, a review).
- Channels are the **event-driven front door**. A conversation between people turns into work,
  and that work is handed to agents or workflows. Their results come back to the channel as
  messages.
- What happens in a channel stays inspectable through the tools we already have. An agent's
  reply links to its run (the conversation, events, instructions and tokens), and a workflow it
  starts links to the workflow run's graph.

Out of scope for v1: email/push notifications, file uploads into channels, reactions, message
search, and invites for other users. The schema shouldn't rule any of these out (see
[D1](#d1-tenancy-who-owns-a-channel-and-the-agents-in-it)).

## What we have today

| Piece | Today | What channels need from it |
| --- | --- | --- |
| Documents chat (`api/chat.py`, `components/Chat.tsx`) | Stateless. The client sends the whole conversation each turn and nothing is saved. Mounted at `/`, kept alive by `Shell`. | Replaced. Its agent becomes something you can add to a channel (see [D13](#d13-what-happens-to-the-existing-chat-and-agent-runs)). |
| Agent runs (`AgentRun`, `checkpoints.py`, `api/agents.py: turn_stream`) | One user and one agent. History lives in a LangGraph thread (`agent-run-<id>`). Turns are serialized by an advisory lock, and a stopped turn keeps what it got to. The turn runs **inside the HTTP request** and streams AI SDK chunks back to it. | Turns have to start from an *event* (a message), not from a request, and stream to *every* viewer of the channel. `turn_stream` needs splitting into "run a turn" and "encode it for one HTTP response". |
| Triggers (`Trigger`, `scheduler.py`) | Start agent and workflow runs without a user. Schedules are the only kind. Uses claim-with-`SKIP LOCKED`, heartbeats and stall sweeps. | A natural place for "when X happens in a channel, start Y" ([D10](#d10-how-chat-turns-into-delegated-work)). The claim/heartbeat pattern carries over to agent invocations. |
| Workflow runs (`workflows.py`, Celery `workflows` queue) | Run on a worker, with heartbeats and no-progress detection. The UI polls. | The model for where channel agent turns run ([D6](#d6-where-agent-turns-run)). Results need somewhere to go back to: a run doesn't know what started it. |
| Ownership | Every table is scoped by `owner_id`. There's no notion of sharing. | Multi-user channels break that assumption ([D1](#d1-tenancy-who-owns-a-channel-and-the-agents-in-it), [D11](#d11-whose-authority-does-an-agent-act-with)). |
| Realtime | None. The only live updates are polling (documents every 2s, workflow runs). There are 2 API replicas behind the ALB, and Redis/Valkey is already deployed. | Messages from other members and other agents must show up live, and fan out across replicas ([D7](#d7-realtime-transport-and-fan-out)). |
| Frontend data | Hand-rolled `useLoad`, with `useChat` for streaming. | A per-channel message store fed by a live event stream. `useChat` assumes one assistant per conversation, so it doesn't fit ([D8](#d8-what-a-channel-message-is)). |

## Concepts

- **Channel**: a named conversation with members. Its name is a slug, unique among live channels
  in its scope (`#weekly-review`). It has an optional topic, and can be archived. A **DM** is a
  channel of kind `dm` with no name and exactly two members, for example you and one agent.
- **Member**: a *principal* in a channel, either a user or an agent. Agent members carry settings
  for how they respond ([D3](#d3-when-does-an-agent-respond)).
- **Message**: posted by a principal, or by the system ("Ops Bot joined", "Workflow *Triage*
  finished"). The body is markdown with structured mention tokens. A message may belong to a
  thread ([D5](#d5-threads)). An agent's message links to the run and agent version that
  produced it.
- **Mention**: a structured reference from a message to a principal, stored in its own table, so
  "everything that mentions me" and "what should be invoked" are both indexed queries.
- **Invocation**: one request for an agent to act, caused by a message. It's queued, claimed by a
  worker, kept alive by a heartbeat, and ends with a reply message. It's the channel's equivalent
  of a workflow run, and the unit we dedupe, rate-limit and trace.
- **Causal chain**: every message and invocation records what caused it (`cause_message_id`) and
  how many agent hops away it is from a human (`hop`). Agent-to-agent loops are bounded with this
  ([D4](#d4-agent-to-agent-mentions-and-loop-control)).

## Proposed architecture

### Data model (sketch)

Names are provisional. `scope_id` stands for whatever [D1](#d1-tenancy-who-owns-a-channel-and-the-agents-in-it)
decides (`owner_id` today, or `workspace_id`).

```
channels
  id, scope_id, kind (channel | dm), name (null for dm), topic,
  created_by → users, archived_at, created_at, updated_at
  unique (scope_id, name) where archived_at is null and kind = 'channel'

channel_members
  id, channel_id, member_kind (user | agent),
  user_id | agent_id                    -- exactly one, by check constraint (like triggers)
  role (owner | member)                 -- users only
  respond_mode (mentions | thread | all) -- agents only, D3
  agent_run_id → agent_runs             -- agents only, if D2 = "one run per membership"
  last_read_message_id                  -- users only: unread counts
  joined_at, left_at                    -- soft leave: old messages keep their author

channel_messages
  id (bigserial: the ordering and resume cursor), channel_id,
  thread_root_id → channel_messages     -- D5
  author_kind (user | agent | system), author_user_id | author_agent_id,
  body (markdown with mention tokens, D9), status (streaming | complete | failed | stopped),
  agent_run_id, agent_version_id        -- for agent messages
  workflow_run_id                       -- for "workflow started/finished" system messages
  cause_message_id, hop                 -- D4
  created_at, edited_at, deleted_at

message_mentions
  message_id, target_kind (user | agent), user_id | agent_id
  index on (user_id, message_id) for the "mentions of me" view

channel_invocations
  id, channel_id, agent_id, member_id, cause_message_id, hop,
  status (queued | running | succeeded | failed | skipped),
  reply_message_id, error, heartbeat_at, created_at, started_at, finished_at
  unique (cause_message_id, agent_id)   -- a message invokes each agent at most once
```

Workflow runs gain an optional `origin_message_id`, so their results can be posted back.
Triggers gain a new kind and an optional output channel ([D10](#d10-how-chat-turns-into-delegated-work)).

### Event flow

```
user posts ──▶ POST /api/channels/{id}/messages
                 │ one transaction: message + mentions + invocations (respond rules, hop limit)
                 ├─▶ publish  message.created           ──▶ Redis  channel:{id}
                 └─▶ enqueue  aios.invoke_agent(inv_id) ──▶ Celery "channels" queue

worker ── claim invocation (SKIP LOCKED, heartbeat) ── take the agent run's turn lock
       ── build context (D2) ── create reply message (status=streaming)
       ── stream the turn: publish message.delta chunks ──▶ Redis channel:{id}
       ── finish: store body/status, parse the reply's mentions
            ├─ users mentioned  → mention rows (unread / inbox)
            └─ agents mentioned → new invocations at hop + 1 (D4)
       ── publish message.updated

browser ── one SSE connection per tab: GET /api/events?after=<message id>
        ◀── API replica subscribed to Redis for the user's channels
        ── on reconnect: backfill from Postgres after the cursor, then live
```

### Reuse and refactors

- **Split `turn_stream`.** The core becomes `run_turn(ctx, run_id, messages) -> AsyncIterator[event]`:
  checkpointing, the turn lock, recording unfinished turns, sandbox pre-warm. It has two
  consumers. One is the existing HTTP chat endpoint, which encodes to AI SDK chunks for its single
  response. The other is the channel worker, which publishes those same `UIStream` chunks to
  Redis, tagged with the reply message id. The scheduler already shares `turn_stream`, so it moves
  to the core too.
- **AI SDK chunks travel unchanged** inside `message.delta` events. On the client,
  `readUIMessageStream` from `ai` turns each reply's chunks into a `UIMessage`. Reasoning, tool
  calls and text then render with the same part components as `RunPage`, and we don't write a
  second protocol.
- **Invocations copy `workflow_runs`' lifecycle**: queued, claimed, heartbeat, fail when stalled
  (`workflows.alive`, `fail_stopped_runs`). Ideally that pattern is pulled into a small shared
  helper, since this would be its third copy (schedules, workflow runs, invocations).
- **Mentions → context.** `ChatDeps` gains `channel_id` and `invocation_id`, so channel tools
  (post, mention, start workflow) know where they are.

### API (sketch)

```
GET/POST        /api/channels                        list (with unread counts) / create
GET/PATCH/DELETE /api/channels/{id}                  rename, topic, archive
GET/POST/DELETE /api/channels/{id}/members           add/remove users and agents, respond_mode
GET             /api/channels/{id}/messages?before=&after=&thread=
POST            /api/channels/{id}/messages          body with mention tokens, optional thread_root_id
PATCH/DELETE    /api/channels/{id}/messages/{mid}    edit/delete own messages
POST            /api/channels/{id}/messages/{mid}/stop   stop a streaming agent reply
POST            /api/channels/{id}/read              move the read cursor
GET             /api/mentions                        "mentions of me" inbox
GET             /api/events?after=                   SSE: message.created/.delta/.updated,
                                                     member.*, channel.*, invocation.*
```

### Frontend

- **Sidebar.** A *Channels* section (`# name`, bold when unread, a badge for mentions) and a
  *Direct messages* section sit under the nav. "Chat" in the nav becomes the channel list, and
  *Documents* moves to its own page or into a channel's details.
- **Routes.** `/c/:channelId`, with the thread pane at `/c/:channelId/t/:messageId`.
- **Message list.** Messages are grouped by author and time, with GitHub avatars for users and an
  agent icon plus "v3" for agents. Streaming replies render live. Tool activity is collapsed into
  a summary line ("Searched documents for …", "Ran Python") with **View run** opening the
  existing `RunPage`. System messages link workflow runs to `WorkflowRunPage`.
- **Composer.** `ChatComposer` gains an `@` autocomplete over channel members. Mentioning an
  agent that isn't a member offers to add it. The composer inserts tokens and shows names.
- **Members panel.** Add or remove agents (and later users), and set each agent's respond mode.
- **State.** One SSE connection lives in `Shell` (it already keeps chat mounted across routes) and
  feeds a per-channel message store. `useLoad` was fine for CRUD pages. A live, cursor-paged,
  multi-writer list probably justifies adding TanStack Query or a small store (zustand). Minor,
  but worth deciding up front.
- **Tests.** `e2e/fake-api.ts` needs an SSE fake that can emit events on demand.

## Open decisions

Ordered by how much of the architecture each one changes. Each has a recommendation, but they're
your calls. **D1–D7 need answering before Phase 1.**

### D1. Tenancy: who owns a channel, and the agents in it?

Everything is `owner_id`-scoped today. "Other users in future" means a channel, and the agents,
workflows and documents it can reach, become shared. That touches every table.

- **a. Introduce `workspaces` now.** Each user gets a personal workspace. Channels belong to a
  workspace from day one, and agents/skills/workflows/documents migrate from `owner_id` to
  `workspace_id` later.
- **b. Channels are owned by a user.** Other users join as guests, and only the owner's agents can
  be members. Cheapest, but it bakes in "one user's stuff" and has to be redone for teams.
- **c. Single-user only for now**, with `owner_id` on channels like everything else. Multi-user
  becomes a later migration.

*Recommendation: a, but only for channels at first.* Add `workspaces` and `workspace_members`,
give every user a personal workspace, and scope channels to it. Leave the other tables on
`owner_id` until a second human actually joins. This costs one table now and avoids re-keying
channels later.

### D2. Agent memory: what does an agent see when it's invoked?

This is the most consequential design choice. It decides how agents relate to runs, sandboxes,
tracing and context size.

- **a. One agent run per membership** (`channel_members.agent_run_id`). Each invocation appends
  "what happened in the channel since your last turn" to that run's LangGraph thread as one
  human message (`[#ops] @alice: … / @ReviewBot: …`), then runs a turn. The agent keeps its own
  tool history. `run_python`'s `/work` persists across the channel's life, and `RunPage` shows the
  agent's whole participation. The cost is that the thread grows without bound, so it needs
  compaction or summarisation, and two invocations of the same agent queue behind its turn lock.
- **b. One run per invocation.** Each run gets the last N channel messages (or the thread) as
  context. Stateless and simple. Parallel invocations are fine and nothing grows. But the agent
  forgets its own tool calls between mentions, the sandbox resets every time, and runs multiply.
- **c. One run per (agent, thread).** Like a, scoped to a thread. Context stays focused and each
  thread is a unit of work. It only works well if threads are the norm ([D5](#d5-threads)).

*Recommendation: a for the top level of a channel and c for threads,* so each run's context is the
part of the channel the agent was asked about. Channel messages in Postgres stay the source of
truth, and the LangGraph thread is the agent's private working memory. Compaction can wait
until context limits actually bite, but the delta format should be designed now.

### D3. When does an agent respond?

- **a. Only when @mentioned.** Predictable and cheap.
- **b. When mentioned, plus any reply in a thread it's already in.** It feels conversational,
  without spamming the channel.
- **c. Every message,** with the agent deciding whether to speak (a cheap "should I respond?"
  model call first). This makes "ambient" agents that watch a channel possible, but costs a model
  call per message per agent and is hard to predict.

*Recommendation: b as the default, with c as an opt-in `respond_mode` per membership* added later.
The schema carries `respond_mode` from the start.

### D4. Agent-to-agent mentions and loop control

If agents can mention agents, two agents can ping-pong forever or fan out wide.

- Allow it, but bound it with `hop` (a reply to a human is hop 1, an agent's mention of another
  agent is hop + 1). Stop at `CHANNEL_MAX_HOPS` (say 3) with a system message.
- Add a per-channel cap on concurrent invocations and a daily invocation and token budget.
- An agent can't invoke itself.

*Decide:* whether agent→agent mentions are in v1 at all, and the default limits. *Recommendation:*
include them, with hops capped at 3. They're the core of "delegate to another agent".

### D5. Threads

- **a. Flat channels only.** Simplest UI, but long agent replies and parallel workstreams
  interleave and get hard to follow.
- **b. Slack-style threads** (`thread_root_id`, one level deep). By default an agent replies in
  the thread of the message that mentioned it. Each thread becomes the natural "task" that
  delegated work hangs off.

*Recommendation: b.* The column costs nothing now and is painful to add later. The thread pane
can ship after the main channel view, but agent replies should go into threads from the start.

### D6. Where agent turns run

Today a chat turn runs inside the HTTP request, and a scheduled turn runs in the API process. A
channel turn starts from an event, so it has no request to live in.

- **a. In the API process** (a background task, like the scheduler). No new moving parts. But a
  deploy kills in-flight turns, and the turns compete with request handling.
- **b. A Celery queue (`channels`)** run by the `workflow-worker` image, with the
  claim/heartbeat lifecycle from `workflows.py`. Turns survive client disconnects and scale
  separately. Streaming goes through Redis.
- **c. A dedicated long-running service** (an asyncio consumer). Lower latency than Celery
  prefork for many concurrent streaming turns, but it's a new service to deploy and operate.

*Recommendation: b.* It matches how workflow runs already work, and it lets workflow agent nodes
and channel turns share one execution path. Watch that the worker can stream many async turns
concurrently, since Celery's prefork concurrency is per process. If that becomes the bottleneck,
move to c.

### D7. Realtime transport and fan-out

- **Transport:** SSE (one-way, works through ALB and nginx with buffering off, auto-reconnects,
  and the cookie auth works as-is) or WebSocket (two-way, but every message we send is already a
  POST, so we don't need two-way).
- **Fan-out across replicas:** Redis pub/sub (already deployed, fast, but fire-and-forget) or
  Postgres `LISTEN/NOTIFY` (no new dependency, but there's an 8KB payload limit and it ties up a
  connection per listener).
- **Resume:** the client reconnects with `after=<last message id>`, and the server backfills from
  Postgres before going live. That makes lost pub/sub messages harmless for persisted state. Token
  deltas missed during a disconnect are dropped, and the stored message is fetched instead.

*Recommendation: SSE + Redis pub/sub + DB backfill.* One SSE connection per tab carries all the
user's channels, not one per channel.

### D8. What a channel message is

- **a. Text only.** The body is the agent's final reply text. Tool calls, reasoning and so on
  live in the linked run, behind **View run**.
- **b. Full structured parts.** Store the AI SDK `UIMessage` parts (tool calls, results,
  reasoning) on the message itself.

*Recommendation: a for storage, plus a live view while streaming.* The run's checkpoint is already
the record of tool activity, so duplicating it means two sources of truth. While streaming, the
client renders the full parts from the deltas. Once a message is complete, it shows the text with
a collapsed one-line tool summary, stored as a small `meta` JSON column, not full parts.

### D9. How mentions are encoded

Agents can be renamed, and so can GitHub logins. Agent names are unique only per owner and among
live agents.

- **a. Plain `@name` in text,** parsed when the message is posted. Simple, but renames break old
  messages and names can be ambiguous.
- **b. Structured tokens in the body,** such as `<@agent:12>` and `<@user:3>`, rendered to current
  names. The composer inserts them, and agents are told the format and emit it. A server-side
  fallback resolves bare `@name` written by agents.

*Recommendation: b.* It's what Slack does, and it keeps `message_mentions` exact.

### D10. How chat turns into delegated work

This is the "front door" part. There are several mechanisms, and they compose:

- **a. Agent tools** in the library: `start_workflow` (an allowlist of workflows in the tool's
  settings, so it's versioned like any tool), `post_message` and `mention_user`. The workflow run
  records `origin_message_id`, and a worker posts the result back into the originating thread
  when the run ends.
- **b. Slash commands for humans:** `/run <workflow> <input>`, `/schedule …`. These are
  deterministic and need no model call.
- **c. Channel triggers:** a new `TriggerKind.channel_event`, with filters (a message mentions X,
  matches a pattern, or is posted in a channel), that starts an agent or workflow. Schedules gain
  an optional output channel, so a 9am report posts itself to `#daily`.
- **d. Human-in-the-loop:** an agent mentions a user and *pauses* until they reply or approve
  (LangGraph `interrupt`). This is powerful, but it needs workflow and agent runs that can
  suspend, which they can't today.

*Recommendation: a and c (the schedule output channel) in v1, then b, and d last.* Decide now
whether workflow results always come back to the channel (this needs `origin_message_id` on
`workflow_runs` and a post-back step in `_finish_run`).

### D11. Whose authority does an agent act with?

`ChatDeps.user_id` decides whose documents `search_documents` searches, and whose quota pays. In
a single-user channel it's obvious. When user B mentions an agent owned by user A, it isn't.

- **a. The agent's owner.** B can then read A's documents through the agent. That's a data leak
  unless it's an explicit policy.
- **b. The user who mentioned it.** Safe, but the same agent behaves differently depending on
  who's asking, and agent-to-agent hops have to carry the original human through.
- **c. The channel's workspace** (needs [D1](#d1-tenancy-who-owns-a-channel-and-the-agents-in-it) a):
  agents see the workspace's shared documents only.

*Recommendation: b for now* (in v1 it's always the single user anyway), carried along the causal
chain, then c once workspaces are real. This needs deciding before a second user can join a
channel. It doesn't block Phase 1.

### D12. Agent version in a channel

Runs pin a version, workflows pin versions, and schedules use the current version. Which should
a channel member be?

- **a. Follow the current version.** Each invocation uses the agent's current version, and the
  message records which one replied. With [D2](#d2-agent-memory-what-does-an-agent-see-when-its-invoked)
  a, the membership's run would then have to be re-pinned on upgrade. Upgrade by starting a new
  run that carries a summary across, or allow the run's version to change between turns.
- **b. Pin at join,** with an "Update to vN" action like workflow nodes have.

*Recommendation: a.* A channel is a live workspace, and people will expect an edited agent to
behave as edited. Record the version per message for traceability. This interacts with D2 a: an
`AgentRun` is pinned to one version today, so either relax that or roll the run over on upgrade.

### D13. What happens to the existing chat and agent runs?

- The **documents chat** at `/` becomes a built-in, undeletable "Assistant" agent (prompt plus
  `hybrid_search`). Each user gets a DM with it, so nothing is lost and it gains saved history.
- **Agent runs from the Agents page** either stay as they are (a private, unshared sandbox
  conversation), or become DMs with the agent.

*Recommendation:* keep agent runs and `RunPage` as the inspection and "test this agent" surface.
Every channel reply links into one, and the Agents page's runs tab lists channel-driven runs
alongside the rest. Retire `/api/chat` once the Assistant agent exists.

### D14. Smaller decisions (can wait until their phase)

- **Unread and notifications:** in-app only (unread counts, a mentions inbox, a browser tab badge)
  for v1. Email and push come later.
- **Editing and deleting messages:** does editing a message that mentioned an agent re-invoke it?
  (Recommendation: no.)
- **Stopping an agent:** any member can stop a streaming reply (it maps onto the existing
  stopped-turn handling).
- **Retention:** messages kept forever, like runs. Deleting an agent leaves its messages, with the
  author shown as "deleted agent".
- **Frontend state library:** TanStack Query vs a small store (see Frontend).

## Phases

Each phase is a PR that's useful on its own, in the stack style of the workflow editor.

1. **Channels without agents.** Schema (channels, members, messages, mentions, plus workspaces if
   D1 a), CRUD API, mention-token parsing, and a basic channel UI with the sidebar list and
   composer. Updates poll for now.
2. **Realtime.** Redis pub/sub, `GET /api/events` SSE with backfill, the client event store, and
   unread counts. Polling is removed.
3. **Agents in channels.** Add agents as members. Split `turn_stream` into `run_turn`. Add
   `channel_invocations` and the `channels` Celery queue with heartbeats, streamed replies, links
   to runs, respond modes (D3) and hop limits (D4).
4. **Threads and the full UI.** Thread pane, `@` autocomplete, members panel, collapsed tool
   summaries, stop button, mentions inbox. The Assistant agent replaces the documents chat (D13).
5. **Delegation.** `start_workflow`, `post_message` and `mention_user` tools. `origin_message_id`
   with post-back of workflow results. Schedules gain an output channel.
6. **Channel triggers and slash commands** (D10 b, c).
7. **Later:** a second human user (invites, workspace sharing, D11 c), human-in-the-loop pauses
   (D10 d), notifications, search over channel history.

## Risks

- **Context growth** in long-lived membership runs (D2 a). Mitigation: a delta window plus
  compaction, and a per-turn context budget.
- **Cost runaway** from agent-to-agent fan-out or `respond_mode = all`. Mitigation: hop caps,
  concurrency caps, budgets, and invocations visible in the UI so they're easy to notice.
- **Streaming through Celery.** Prefork workers handle one task per process, so many concurrent
  long turns need a larger pool or an async worker (D6 c).
- **Three copies of the claim/heartbeat lifecycle.** Factor it out in Phase 3 rather than
  copying it again.
