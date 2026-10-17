# Chats: group conversations between users, agents and workflows

Status: **plan, with open questions** (2026-10-17). Replaces the documents chat at `/` as the
Chat section. Organisations (several users in one workspace) aren't shipped; the design leaves
room for them but doesn't build them.

## Goal

A chat is a Slack-style channel. Its members are users, agents and workflows, in any mix. A
message can mention (`@`) any member. Mentioning an agent or a workflow starts a run of it:

1. a short acknowledgement appears in the chat, from the agent or workflow;
2. under it, a card follows the run live: a workflow's card shows the run's graph as the run
   page draws it; an agent's shows time elapsed, tool calls and tokens so far;
3. clicking the card opens the run's page;
4. when the run ends, its result is posted to the chat (see question 6).

Before the run starts, an interim **handoff** agent summarises the chat so far into a brief
that's prepended to the run's input (a workflow run's `input`; an agent run's first message).

Public chats can be joined by anyone signed in; private chats are invite-only.

## What the codebase already gives us

- **Starting runs and recording how they went** is already shared between the UI and schedules:
  `new_agent_run` + `turn_stream` (`api/agents.py`) run one turn of an agent's current version
  with a given message; `new_workflow_run` + `queue_workflow_run` (`api/workflows.py`) queue a
  workflow run for the worker. The scheduler (`scheduler.py`) is a worked example of starting
  both from outside an HTTP request, with a concurrency cap, heartbeats and stall detection. A
  chat mention is the same operation with a different cause.
- **Live run state** is in the database and polled: `workflow_runs` and `workflow_run_steps`
  are read every second by `WorkflowRun.tsx` (`usePoll`, `nodeStates`, `WorkflowCanvas`), which
  is the UI the workflow card embeds. Agent runs have no status or counters yet (a run is a
  thread; token usage sits inside its checkpoints as `usage_metadata` per model response).
- **The AI SDK stream** (`ui_stream.py`) turns a turn into UI chunks; `UIStream.feed` sees every
  model response and tool result as the turn goes, so it's where per-turn counters can be kept.
- **`Trigger`** records what started a run (`agent_runs.trigger_id`) and its outcome
  (`last_status`, `last_run_id`, `last_workflow_run_id`). Its `kind` enum was built to grow, but
  a trigger is a standing rule that fires again and again, and a mention is one event; the
  chat gets a row of its own per mention instead (`chat_invocations`, below), shaped like a
  trigger's outcome fields.
- **Ownership** is per user everywhere (`owner_id`, `get_owned_agent`, `get_owned_run`).
  `ChatDeps.user_id` is the identity a run executes as: it scopes `search_documents` and the
  sandbox. Nothing is shared between users today, which is the crux of questions 1 and 2.
- **The documents chat** (`/api/chat`, `Chat.tsx`) is stateless and client-side; the Shell
  keeps it mounted across navigation so the conversation survives. Persisted chats remove the
  need for that.
- **Names** are unique per owner among live agents, and separately among live workflows, so an
  agent and a workflow can share a name (question 8).

## Design

### Data model

```
chats              id, owner_id, name, description, visibility (public | private),
                   created_at, updated_at
chat_members       id, chat_id, kind (user | agent | workflow),
                   user_id | agent_id | workflow_id (exactly one, like triggers' target),
                   role (owner | member; users only), added_by, added_at
                   unique (chat_id, user_id), (chat_id, agent_id), (chat_id, workflow_id)
chat_messages      id, chat_id, author_kind (user | agent | workflow | system),
                   author user_id | agent_id | workflow_id (null for system),
                   kind (text | run_card), body (markdown),
                   mentions JSONB [{kind, id, name}], invocation_id (run cards),
                   created_at; index (chat_id, id) for `?after=` reads
chat_invocations   id, chat_id, message_id (the mentioning message), requested_by (user),
                   target_kind, agent_id | workflow_id,
                   status (queued | briefing | running | succeeded | failed),
                   brief (the handoff summary), error,
                   agent_run_id, workflow_run_id (SET NULL: a deleted run leaves the card),
                   created_at, started_at, finished_at
agent_runs      +  status (running | succeeded | failed | null for runs that predate this),
                   started_at, finished_at, tool_call_count, input_tokens, output_tokens
```

Agents and workflows are soft-deleted, and deleting one deletes its runs: a card whose run has
gone says so (`agent_run_id` is null) rather than disappearing. Removing a member leaves what
they posted. Deleting a chat deletes its messages and invocations; the runs stay with their
agent or workflow, as a workflow's runs outlive the workflow today.

The counters go on `agent_runs` rather than on the invocation because they're useful beyond
chat (the run list, the Insights page) and because a run is where the turn already writes
(`_end_turn`). They're cumulative over the run's turns.

### A mention, end to end

1. `POST /api/chats/{id}/messages` with the text and the mentions the composer resolved
   (`[{kind, id}]`, chosen from autocomplete, so names needn't be parsed server-side). The
   message is stored. For each agent or workflow mentioned that is a member (a mention of a
   non-member is a 422; question 8 covers auto-adding) an invocation is created, `queued`, and
   an acknowledgement message is posted by that member with a `run_card` referencing it.
   One request, one transaction, so the chat never shows a mention without its card.
2. A background task per invocation (same process, as the scheduler fires agents today, or
   a Celery task: question 3) runs the **handoff** step: `briefing`. It calls a small model once,
   with the chat's recent messages and the mentioning message, for a structured result
   `{ack, brief}`. The ack ("On it: I'll check the Q3 figures against the plan…") replaces the
   placeholder text in the acknowledgement message; the brief is stored on the invocation.
   One call gives both, so the acknowledgement costs nothing extra.
3. The run starts with `brief + "\n\n" + message text` as its input: `new_workflow_run` and
   `queue_workflow_run` for a workflow; `new_agent_run` then one turn through `turn_stream`
   (its chunks consumed and dropped, as the scheduler does) for an agent. The invocation
   records the run id and goes `running`. A run that can't start (409, 422, 503 from those
   helpers) fails the invocation with that message, and the card says so.
4. While it runs, the card is live. The chat's poll (`GET /api/chats/{id}/messages?after=<id>`)
   returns new messages and, in the same response, the state of every invocation in the chat
   that's not finished: status, and for agents the run's counters and `started_at`, for
   workflows the run summary and steps (what `GET /api/workflows/{id}/runs/{run_id}` returns),
   so ten live cards cost one request a second, not ten. The agent card derives elapsed time
   client-side from `started_at`, as `WorkflowRun.tsx`'s `duration` does.
5. When the turn or run ends, the member posts its result as an ordinary message (the agent's
   final reply; a workflow's `output`, or its error), the invocation goes `succeeded` or
   `failed` and the card stops updating.

Agent runs started from a chat run under the scheduler's protections: the same concurrency
cap (`scheduler_max_concurrent_runs`, or one of its own), heartbeat and stall handling, so a
deploy mid-turn fails the invocation rather than leaving the card spinning for ever. Workflow
runs already have this in the worker.

Mentions inside a reply posted by an agent or a workflow don't trigger anything in v1, so two
agents can't set each other off. A later version can allow it with a depth limit.

### Counters on agent runs

`turn_stream` already loops over the graph's `updates`: a model response (`AIMessage`, with
`usage_metadata`) and each tool result (`ToolMessage`). A `TurnStats` accumulator fed from the
same place increments `tool_call_count` and adds input and output tokens, and `_end_turn`
grows to write them with `status`, `started_at` and `finished_at`. Writing at each model step
rather than each chunk keeps it to one small update per step. "Tokens" on the card is input
plus output tokens as the provider reports them per model call (so input tokens count the
context again at each step, as bills do); cache read tokens are shown separately if at all.

### API

```
GET    /api/chats                         mine and joined; public ones I'm not in, flagged
POST   /api/chats                         {name, description, visibility}
GET    /api/chats/{id}                    with members
PUT    /api/chats/{id}                    name, description, visibility (owner)
DELETE /api/chats/{id}                    owner
POST   /api/chats/{id}/join               public chats
POST   /api/chats/{id}/members            {kind, id}; agents/workflows I own, users by id
DELETE /api/chats/{id}/members/{member_id}
GET    /api/chats/{id}/messages?after=N   messages after N, plus `invocations` still live
POST   /api/chats/{id}/messages           {body, mentions: [{kind, id}]}
GET    /api/chats/{id}/mentionable        members, for the composer's autocomplete
```

A run's page (`/agents/{id}/runs/{run_id}`, `/workflows/{id}/runs/{run_id}`) must open for a
chat member who isn't the agent's owner, or the card's link is dead for them: see question 1.

### Frontend

- A **Chat** section at `/chats` with the master-detail layout the other sections use: the
  list of chats, and the open chat (messages, composer, a members panel). `/` redirects to the
  most recent chat. `Chat.tsx`'s mount-across-navigation trick goes; the server has the history.
- The **composer** is `ChatComposer` plus `@` autocomplete over `mentionable`, inserting a token
  the renderer turns into a chip (question 8 for the format).
- **Messages** render with `MarkdownView`. Author chips by kind (avatar for users, the Bot and
  Workflow icons the nav uses). A `run_card` message renders `RunCard`:
  - workflow: `RunStatusBadge`, elapsed, and a read-only `WorkflowCanvas` at card size with
    `nodeStates`, the same drawing the run page makes (question 11);
  - agent: `RunStatusBadge`, elapsed (ticking), tool calls, tokens;
  - both link to the run page; a card whose run was deleted says so.
- The open chat polls `messages?after=` with `usePoll` while the tab is visible, or an
  invocation is live, and backs off when neither (question 5).
- The browser tests get chats in `e2e/fake-api.ts`, including a scripted invocation that moves
  through `briefing`, `running` and `succeeded` so the card is tested without a model.

## Open architectural questions

Each with the options as I see them and a recommendation. The first three shape the schema and
need deciding before phase 1; the rest can be settled as their phase comes.

1. **Whose identity does a run execute as, and who may open it?** Today a run executes as its
   agent's owner (`ChatDeps.user_id`: their documents, their sandbox) and only the owner can
   read it. In a shared chat, member B mentions A's agent.
   - (a) As the agent's owner: the agent behaves the same for everyone, but B's question is
     answered from A's documents, and A pays. Run pages need a "visible to this chat's
     members" rule.
   - (b) As the requester: the agent searches B's documents. Surprising for a shared agent
     ("why does it not know the file A uploaded?"), and the run then belongs to B under A's
     agent, which no table expresses.
   - (c) As the chat: documents shared into a chat. The cleanest model and the biggest build.
   Recommendation: (a), with run pages readable by any member of a chat whose invocation points
   at the run, and a note that (c) is where organisations likely take this. Until organisations
   ship there's one user per deployment and this is moot in practice, but the schema should
   not preclude it: `requested_by` on the invocation is kept distinct from the run's owner.

2. **What does "public" mean before organisations exist?** Every signed-in user of the
   deployment can see and join it, which today is one person. Recommendation: build visibility
   and join now (they're cheap and the UI needs them), define public as "all users of this
   deployment", and expect organisations to narrow it to "all users of this organisation".
   Rate limiting and cost control (a public chat is a way to spend someone else's model
   budget) are part of the organisations work, and question 1's answer.

3. **Where does a chat-triggered agent turn run?** A workflow run already goes to the Celery
   `workflows` queue. An agent's turn runs in the API process today, either bound to the request
   streaming it or, for schedules, in a background task with a heartbeat.
   - (a) In the API process, reusing the scheduler's firing machinery (cap, heartbeat,
     `fail_stalled`): least new code, and it's proven. Turns compete with chat requests for the
     process, which the cap is for.
   - (b) A Celery task (`aios.run_agent_turn`) on the `workflows` queue: survives API deploys
     without special handling, scales separately, but the heartbeat and stall code in
     `workflows.py` is per workflow run and would need generalising; and `turn_stream` would be
     consumed in a worker that has no HTTP response, which is fine (the scheduler does it) but
     makes the sandbox pre-warm and `_record_turn` paths run somewhere new.
   Recommendation: (a) for v1, factoring the scheduler's `fire` so both schedules and
   invocations call it, and move to (b) when the Insights or organisations work needs the API
   process to stay light. The invocation table doesn't care which.

4. **One run per mention, or one thread per (chat, agent)?** The product brief implies a run
   per mention, with the handoff brief carrying context. The alternative keeps one agent run per
   agent per chat and sends each mention as a new turn of it, so the agent remembers the chat
   itself (and `checkpoints.turn`'s advisory lock serialises two members' mentions). Per-mention
   runs are simpler, match workflows (which can't have threads), and keep the run page an
   accurate record of one task. Per-chat threads give better continuity and fewer
   summariser calls, but grow without bound and conflate every task into one page.
   Recommendation: per mention for v1, as briefed; revisit once we see how good the brief is.

5. **Realtime transport.** Polling is the codebase's one pattern (`usePoll`, 1s). Several open
   chats, each polling, plus live cards is fine at this scale; Postgres `LISTEN/NOTIFY` feeding
   an SSE endpoint per chat is the upgrade. Recommendation: poll `messages?after=` with a
   back-off when nothing is live and the tab is hidden, and shape the response so an SSE stream
   can carry the same payload later. No WebSockets: nothing here is client-to-server realtime.

6. **Does the result come back into the chat, and does it stream?** The brief stops at the card.
   A chat whose agents never answer in the chat is a launcher, so I've assumed the final reply
   or output is posted as a message. Streaming the reply token by token into the chat would
   mean a per-invocation stream alongside the poll; the card's counters already show progress.
   Recommendation: post the result as a message when the run ends; no token streaming in v1.
   Long replies are posted whole (markdown), with the run page for the detail.

7. **The handoff summariser.** Decide: how much chat goes in (the last N messages, everything
   since the target last ran, or the whole chat); which model (a small, fast one; the agent
   version's `model` is the agent's choice, not the chat's); whether it also runs for agents
   (the brief says workflows; an agent could instead be given the raw recent messages as its
   first turn, which is cheaper and loses nothing); whether to skip it when the message
   mentions nothing earlier; and whether the brief is shown in the chat (as a collapsible
   section on the card) so members can see what the agent was told. Recommendation: last 30
   messages or 8k tokens, whichever is less; the deployment's default model with a tight system
   prompt and structured output `{ack, brief}`; run it for both kinds for one code path; never
   skip (the ack comes from it); show the brief on the card. Trace it to Langfuse with the
   invocation id, tagged `handoff`.

8. **Mention format and name collisions.** Users have `login`; agents and workflows have names
   unique within their kind, so `@Review` can be an agent and a workflow. Options: the composer
   resolves mentions and the message stores them structurally (`mentions` JSONB), with the text
   carrying a token like `@[Review](agent:12)` the renderer turns into a chip; or plain `@Review`
   text with server-side resolution and a disambiguation rule. Recommendation: structured
   mentions chosen from autocomplete, which also makes "mention a non-member" a UI impossibility
   rather than a 422 to explain; the autocomplete groups by kind. Mentioning a non-member via
   the API is refused rather than auto-adding, so membership stays an explicit act.

9. **The documents chat.** `/` is a stateless assistant over the user's documents. Options:
   keep it as a separate page; make it a built-in agent that's a member of every chat ("aios");
   or drop it, since any agent can be given the document search tool. Recommendation: make it
   a built-in member of each new chat, so a fresh chat is useful at once, and retire `/api/chat`
   once it is. Its runs get the same card.

10. **Where the agent card's numbers come from.** Counters on `agent_runs` written per model
    step (above), or derived at read time from the thread's checkpoints. Reading the thread on
    every poll of every live card is too heavy, so counters; the question is whether they also
    backfill for old runs (no: null means "not tracked", and the run page keeps computing from
    the messages as it does). Also whether "tokens" means input + output per call summed (bills)
    or the final context size; recommendation above is the sum.

11. **The workflow card's size.** The brief says the card shows the existing live run UI. A full
    `WorkflowCanvas` in a chat message is large and interactive (pan, zoom, click to inspect).
    Recommendation: the canvas, read-only, in a fixed-height card with the inspector disabled
    and a step-count line ("3 of 5 nodes done"), clicking anywhere opens the run page; a
    subworkflow node doesn't drill down inside the card. If it proves too busy, fall back to
    the status strip plus step list.

12. **Agents acting in the chat.** Should an agent run from a chat have tools to read more of
    the chat or post interim messages? And should an agent's reply mentioning another member
    trigger it? Both are powerful and both are loops waiting to happen. Recommendation: neither
    in v1; the brief is the agent's window on the chat and the result message is its only post.
    Later: a `chat_history` tool and mention-chaining with a depth limit on the invocation.

13. **Deletion and retention.** Deleting a chat: its messages and invocations go; the runs stay
    (they're the agent's). Deleting an agent: its runs go (today's rule), cards keep the
    invocation and say the run was deleted. Removing a member: nothing is deleted. Leaving a
    public chat and rejoining: history visible (it was public). Recommendation as stated;
    private-chat history for members added later is visible from the start, as in Slack.

14. **Notifications and unread.** A user mention has nothing to notify through yet. Recommendation:
    v1 keeps an unread count per (chat, user) from the last message id seen, shown in the chat
    list; mention highlighting in the message; no email or push. Organisations decide the rest.

## Phases

Each a PR on its own; the earlier ones are useful alone.

1. **Chats and messages.** Migration (`chats`, `chat_members` for users, `chat_messages`),
   CRUD and messages API with `?after=`, the Chat section with list, room, composer and
   polling. `/` redirects to chats. The documents assistant stays as is for now. Tests: API
   (membership, visibility, `after` cursor) and a browser test against the fake API.
2. **Members of other kinds and invocations: workflows first.** Agents and workflows as
   members, autocomplete and structured mentions, `chat_invocations`, the acknowledgement and
   card, the chat-level live state in the poll response, the workflow card embedding the
   canvas, the result posted back. Workflows first because their live state exists already;
   the invocation runs without a brief (the message is the input) so the handoff can land in
   its own PR. Run pages readable by chat members (question 1).
3. **Agent invocations and counters.** `agent_runs` status and counters written from
   `turn_stream`; the scheduler's firing machinery factored so an invocation uses it; the
   agent card. The run list and page show the counters too.
4. **The handoff agent.** The `briefing` step, structured `{ack, brief}`, the brief shown on
   the card, traced to Langfuse. Settings for the model and the context window.
5. **Public chats.** Visibility, join, the unread count, and the documents assistant as a
   built-in member (question 9) if agreed. What organisations change is written up as they
   arrive.
