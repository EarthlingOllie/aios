# 0001: Bring the platform together around the organisation's database

- Status: accepted
- Date: 2026-11-07

## Context

aios exists so that teams can work together on a platform that brings all the context they need
into an isolated database, owned by a set of managed agents and workflows.

A review of the repository at `256a2d8` found that the parts are well built but sit beside each
other rather than forming one loop:

- Each organisation's data store holds its uploaded documents, the tables its agents ask for and,
  optionally, a one-way copy of its run history. Chats, mentions, handoff briefs, runs,
  agents, workflows, schedules and approvals all live in the shared platform database. The
  team's richest context, its conversation, can't be searched by its agents.
- Context comes in only as uploaded files. No connectors, webhooks or external tool servers.
- Documents, run history and data tables are three silos, with a search tool each.
- Agents are stateless between mentions. Nothing records which agent owns which tables, and
  every agent that has `write_data` can write every table.
- Results rarely come back to the team. A scheduled run's output stays on its run page, and the
  only notifications are for schema change requests. The only trigger kind is `schedule`.
- Workflow edges carry plain text. Workflows have no branches and no human approval step other
  than the schema change pause.
- There are two chat experiences: the one-to-one agent run chat (streamed) and the group Chats
  (polled every second).
- Governance is a stopgap: an organisation's creator is its only role, and any member approves
  any schema change, drops included.
- Several features cost far more code than they return: the run history export (an outbox,
  backfill, separate migration branch and two settings, which exist only because runs live
  outside the store), hard-deleting agents with a dependents resolution flow, and the "one
  organisation per user, always a personal one" rule with its empty-organisation join logic.

We adapt the existing code rather than rewrite it. Store provisioning, per-store roles, quotas,
schema change approval, versioning and pinning, pause and resume, the firing machinery and the
test suites are the expensive parts, and they suit the aim.

## Decision

The organisation's data store becomes the system of record for everything the organisation
makes, says and produces. The platform database keeps only who people are, which organisations
exist, and how to reach and schedule work in each store.

The work is in two parts: a refactor that removes what the new direction makes unnecessary and
simplifies what's left, then the adaptation itself.

### Part A: Refactor

#### R1. Remove the run history export

Runs will live in the store (D1), so a copy of them there is redundant. Remove:

- `aios.datastores.history`, the `history_exports` outbox and the `aios.export_history` and
  `aios.set_up_history_export` tasks with their beat entries;
- the store tree's `history` branch (`h0001`, ...) and the `history` schema, with a store
  migration that drops it;
- the `history_*`, `export_run_history` and `embed_run_history` columns on `data_stores`;
- the Run history export settings (`RunHistoryExport.tsx`, its API and its tests).

Tool keys are never renamed, so `history_search` stays registered. Under D1 it becomes the
unified search restricted to run messages. R1 therefore lands as the first step of D1, so that
agent versions using `history_search` never lose their tool.

#### R2. Archive agents instead of deleting them

An agent can be archived and unarchived. An archived agent:

- starts no new runs: by hand, by schedule, by mention, or as a workflow node (starting a
  workflow that pins it is refused, as running a deleted agent is today);
- lets runs already under way finish, rather than being cancelled;
- has its schedules paused, and leaves chats' mention lists while staying in their member
  lists, marked archived;
- keeps its name reserved, so unarchiving can't clash.

Remove the hard delete and its resolution flow: the dependents check with run cancellation and
workflow deactivation (`aios.dependents`'s cancel paths, `DELETE /api/agents/{id}`'s 409
protocol, `AgentDeleteDialog`). Keep a read-only "used by" list on the agent's page, so people
can see what archiving affects. Workflows and skills get the same archive model, for
consistency. Rows are deleted outright only when their organisation is.

#### R3. Many organisations per user, made only on request

- Drop the unique index on `organisation_members.user_id`. A user may belong to any number of
  organisations.
- Signing up makes no organisation and no store. A user in none lands on a page that creates one
  or explains how to join one. A store is provisioned when its organisation is created.
- An invitation simply adds a membership. Remove the empty-organisation rules: the "empty"
  check, deleting the user's personal organisation as they join, and listing what blocks the
  join.
- Leaving removes the membership and nothing else. No new personal organisation is made. The
  last admin can't leave while others remain (see D8), and the last member deletes the
  organisation instead of leaving it.
- The organisation is named in the URL. The SPA's routes move under `/o/<slug>/...` and the
  API's under `/api/o/{organisation_id}/...`, so two tabs can work in two organisations and a
  shared link opens the right one. One dependency still resolves it (`CurrentOrganisation`, now
  from the path, checking membership). It stays the only place this happens. `GET /api/me`
  lists the user's organisations. The shell gets a switcher.
- Existing personal organisations become ordinary organisations. Nothing is migrated away.

### Part B: Adaptation

#### D1. The store is the system of record

**What stays in the platform database:**

- users, sign-in sessions, organisations, memberships with their roles (D8), and invitations;
- the `data_stores` registry and its encrypted credentials;
- connector secrets (D2);
- per-user notifications (D4), which hold references, not content;
- cross-organisation coordination indexes: what's due to fire (D3 schedules), what's queued,
  and which stores have events waiting. These are thin rows pointing into a store.

**What moves into the store:**

- agents, skills, workflows and all their versions;
- agent runs, workflow runs and steps, and the LangGraph checkpoints (one checkpointer per
  store, in a `langgraph` schema there);
- chats, chat members, messages, mentions, invocations and handoff briefs;
- trigger definitions;
- schema change requests and approvals (D7, D8);
- agent memory (D5);
- the audit log (D8);
- sources and what they sync (D2);
- saved Insights charts (D9).

Documents, chunks and `data` tables are already there.

**People.** A store has no users table. It gets `aios.people`: a mirror of the organisation's
members (user id, name, role), kept up to date by the platform on membership and profile
changes. Agents can then answer "who" from the store alone, and content keeps its author ids
without foreign keys across databases.

**Migration.** Expand and contract, organisation by organisation, in `migrate-all`:

1. Make the store tables.
2. Copy each organisation's rows into them, keeping ids, along with their checkpoint threads.
3. Switch reads and writes to the store.
4. In a later release, drop the platform tables.

**Connections.** Moving everything onto stores multiplies connections: organisations ×
processes × roles. Put a pooler in front of the stores (PgBouncer, or RDS Proxy on AWS) before
step 3. Transaction pooling breaks session advisory locks, so the turn lock that serialises a
run's turns moves to a row lock on the run (`SELECT ... FOR UPDATE` held for the turn on a
dedicated session) or a Redis lock.

**Unified search.** One index in the store, `aios.search_items`. Each item records:

- its kind: document chunk, chat message, run message, brief, memory or synced source item;
- a reference to its row;
- its text, `tsvector` and embedding;
- when it was written;
- its visibility: the whole organisation, or one private chat.

Each writer indexes what it writes, in the same transaction. Embedding happens afterwards,
under the budget the history embedder uses now.

A new tool, `search`, takes a query and optional filters (kinds, time range, chat, agent,
source) and runs the existing RRF search over the index. Visibility is enforced from the run's
context: a private chat's content is found only by runs started in that chat. `hybrid_search`
and `history_search` stay registered, as `search` fixed to their kinds. The Search page becomes
the people's view of the same index, showing only what the signed-in member may read.

**Retention.** Each organisation chooses how long runs and chats are kept before they're
pruned. These are two admin settings, `run_retention_days` and `chat_retention_days`, and each
offers **Keep indefinitely**, which is the default.

- **Runs:** a daily job deletes runs that finished longer ago than the setting, with their
  steps, checkpoint threads and search index entries.
  - Runs still going, or waiting on a request, are never pruned.
  - An agent's continuing run in a chat (D5) is pruned turn by turn rather than whole.
- **Chats:** the same job deletes messages older than the setting, with their mentions,
  invocations, briefs and handbacks, and their search index entries.
  - The chat itself stays.
  - A message whose invocation is still going is kept until it finishes.
- **Untouched:** pruning never touches agent memory, `data` tables, documents or the audit log.
  The audit log records each prune: what kind, how many, and up to when.

Synced items (D2) don't follow this setting. They're copies of content whose system of record
is elsewhere, and they follow their source instead.

#### D2. Sources and connectors

A **source** is something that brings content into the store. Its kinds:

- uploads (today's documents);
- web pages and URLs, re-fetched on a schedule;
- incoming webhooks (shared with D3);
- an email inbox;
- **connectors** to other systems.

A **connector** is one connection to an external system, with credentials, and it can serve two
purposes:

- **Sync:** pull content into the store on a schedule, incrementally from a stored cursor. Each
  item keeps its source, external id, URL and updated time. It's chunked, embedded and indexed
  as a `source_item`, and found by `search`.
- **Tools:** expose the system's actions to agents during a run.

We implement connectors as MCP clients wherever a server exists (Slack, Google Drive, GitHub,
Notion, Atlassian and others), rather than writing one integration each. Native adapters are
for what MCP can't do well, such as bulk sync.

Credentials (OAuth tokens or API keys) are stored encrypted in the platform database. The key
derivation used for store credentials applies. They never enter the store, where agents can
query.

A connector's tools reach agents through one registered tool kind, `connector`. Its settings
name the connector and the tools allowed. An agent version pins those, and the tools' schemas
are fetched when a run starts. A connector removed later makes that version unrunnable, as a
tool that left the library does today. This keeps "tools are code" true of the library, while
each organisation chooses its own connectors.

Syncs run on their own Celery queue (`sources`). Only admins add or remove connectors (D8).

**What synced items are.** The store is the system of record for everything that happens in
aios. Content synced from Slack, Drive or GitHub is still owned by the system it came from, so
the store holds a searchable copy, and the copy follows its source:

- an item edited upstream is updated at the next sync;
- an item deleted upstream, or that the connector can no longer see, is deleted at the next
  sync, which reconciles against what the source still lists;
- removing a connector deletes everything it synced, unless the admin chooses to keep it as
  ordinary documents.

What agents produce from synced content (messages, memory, rows in `data` tables) is the
organisation's own and stays.

**First connectors:**

- **Slack:** channels the admin picks, synced as messages with their threads. A
  `post_to_slack` tool sends to an allowed channel.
- **Google Drive:** folders the admin picks, synced as documents through the existing ingest
  pipeline.
- **GitHub:** repositories the admin picks, synced as issues, pull requests and their comments,
  and optionally README and docs files, with tools to comment and open issues.

#### D3. Event triggers

`TriggerKind` gains `event`. An event trigger names one event type and a filter:

| Event | Filter |
| --- | --- |
| `document.ingested` | source, file type |
| `table.written` | table, operation |
| `schema_change.applied` | table |
| `chat.message` | chat, author kind, mentions, keyword |
| `webhook.received` | webhook |

Events are written to an outbox in the store, `aios.events`, in the same transaction as the
change that causes them. A dispatcher matches them against triggers and fires the matches
through `aios.firing`, under the same cap and heartbeats as schedules. A committed event also
sets a flag on its store in the platform database, so the dispatcher finds stores with work
without scanning them all.

- `table.written` is emitted by the code paths that write, `write_data` and syncs, not by
  database triggers.
- Each webhook trigger has its own URL and an HMAC secret. Its payload becomes the run's input,
  checked against the workflow's input schema (D7).
- Loops are prevented by the causation chain. Every run and event carries the chain of runs
  that led to it. A trigger doesn't fire for events its own target caused unless it says so,
  and chains are capped in depth.
- Bursts are coalesced. A trigger can set a debounce window, after which it fires once with
  the batch.

#### D4. Delivering results, and notifications

- A new `post_message` tool lets an agent post markdown to a chat it is a member of. As today,
  mentions in what agents and workflows post start nothing.
- Triggers get a **deliver to** setting, a chat. A scheduled or event-fired run then posts its
  output or its failure there without any prompting.
- **Handback.** Results returning to a chat go through a handback step, the counterpart of the
  handoff brief.
  - What it summarises: a workflow's output, a trigger's delivery, a `start_workflow` result
    posted later, and any output longer than about a screen or in JSON.
  - It reads the output along with the brief and message that started the run, and posts a short
    answer to what was asked.
  - The full output is kept on the invocation, behind a disclosure on the run card, and links to
    the run.
  - It uses the handoff model and is traced the same way (tagged `handback`).
  - If it fails, the raw output is posted with a note saying so, rather than nothing.
  - An agent's reply to a mention is not summarised. The agent was briefed and already writes
    for the chat, and its reply streams straight in (D6).
- Notifications cover more kinds:
  - `run_failed`: to whoever started the run. For a trigger's run, to the trigger's creator and
    the admins.
  - `reply`: to whoever mentioned the agent or workflow, when its result is posted.
  - `mention`: to a person mentioned in a chat.
  - `approval_needed`: to the members allowed to decide a schema change request or a workflow
    approval step (D7, D8).

  Notifications stay per user in the platform database, so one bell covers every organisation
  the user is in. Each holds the organisation and a reference, never content. Email delivery
  and per-kind preferences come later.

#### D5. Agents as persistent team members

**Continuity.** An agent keeps one run per chat. Each mention is a new turn of that run, not a
new run. The handoff brief becomes "what has happened in the chat since your last turn".

Over weeks that thread outgrows the model's context, so older turns are compacted:

- When the thread passes a token threshold, the turns before the most recent few are replaced,
  in what the model sees, by a summary written by the handoff model.
- The latest turns stay verbatim. Summaries are summarised again as they grow.
- The full turns stay in the store (and in search) until retention prunes them, so compaction
  loses nothing a person or `search` can reach. Only the model's working context is
  shortened.

**Memory.** `aios.agent_memory` in the store holds an agent's memories: content, embedding, the
run that wrote it, and created and updated times. The `remember` and `forget` tools write it,
and `search` with kind `memory` reads it, only the agent's own. People see, edit and delete an
agent's memories on a Memory tab on its page.

**Table ownership.** A table created by a schema change an agent requested is owned by that
agent, recorded in `aios.table_ownership`. The owner may write it; other agents may read it.
Admins can change both on the Data page (D8). Postgres ownership stays with `_owner`, because
DDL only ever comes through approved schema changes. "Owns" here means stewardship plus a write
grant.

**Per-agent permissions.** Each agent gets its own database role in the store, `ds_<id>_a<agent
id>`, granted exactly what ownership and sharing give it. Its data tools run as that role.

The model writes the SQL that `query_data` runs, so the role must be one that SQL can't escape:

- `SET ROLE` from a shared login role isn't enough, because `RESET ROLE` or
  `set_config('role', ...)` would undo it.
- Per-agent login roles, with credentials generated and stored as the store's are, opened per
  turn through the pooler, meet this.

The exact mechanism is settled in this item's own plan and must come with a test that tries to
escape it.

#### D6. One chat experience, streamed

- Remove the one-to-one agent run chat (the `useChat` conversation on the run page). A
  conversation with one agent is a **direct message**: a private chat with you and the agent,
  which works like every other chat.
- Run pages become read-only: the trace, events and usage, linked from run cards.
- Chats stream. `GET /api/o/{organisation}/chats/{id}/stream` is a server-sent events stream of
  new messages, invocation updates and the token deltas of replies being written. A reply
  appears as a draft message that fills in as it streams.
- Events are published through Redis pub/sub, which works across processes and stores, where
  `LISTEN` on every store wouldn't.
- The `?after=N` read stays, for catching up after a reconnect. The one-second poll goes. The
  `ui_stream` adapter is kept where it turns LangGraph's stream into deltas.

#### D7. Structured workflows

- **Typed I/O.** A workflow version and each of its nodes may declare an input and an output
  JSON Schema.
  - An agent node with an output schema is run with structured output and checked against it.
  - Without one, output is text, as today, so existing versions keep working.
  - A node's input is a template that can refer to its predecessors' fields
    (`{{summarise.points}}`).
  - A run's input is checked against the version's input schema, and so are webhook payloads
    (D3).
- **Conditional edges.** An edge may carry a condition on its source node's output, written in
  JSONLogic (data, not code, so nothing model-written is evaluated).
  - A node starts once each of its predecessors has finished (succeeded or been skipped) and at
    least one incoming edge is active.
  - A node with no active incoming edge is skipped, and skipping propagates.
  - This replaces "starts once all its predecessors have succeeded". Graphs without conditions
    behave as before.
- **Approval node.** A new node kind, `approval`, has a prompt and the role allowed to decide
  (D8).
  - Reaching it creates an approval request carrying its input, notifies the approvers (D4), and
    makes the step `waiting`, exactly as a schema change wait does now.
  - Deciding resumes the run. The node outputs `{approved, note, decided_by}`, which conditional
    edges can route on.
  - Schema change requests and approval steps share one requests mechanism: the waiting, expiry,
    notification and resume code.
- **`start_workflow` tool.** Its settings name the workflows an agent may start. It starts a run
  with structured input and waits up to a set time for the output. If the run takes longer, it
  returns the run's id, and the result is posted to the chat the turn came from (D4). The
  causation chain (D3) caps depth and refuses cycles, such as an agent starting a workflow that
  mentions it.

#### D8. Governance

**Roles.** Each membership has a role: `admin`, `member` or `viewer`. The creator stopgap goes.
Existing creators become admins, and everyone else becomes a member.

| | Admin | Member | Viewer |
| --- | --- | --- | --- |
| Read everything they can see, search | ✓ | ✓ | ✓ |
| Post in chats, mention agents and workflows | ✓ | ✓ | ✓ |
| Start runs from agent and workflow pages | ✓ | ✓ | |
| Create and edit agents, skills, workflows, triggers | ✓ | ✓ | |
| Archive, roles, invitations, connectors, organisation settings | ✓ | | |

**Approval rules.** Each kind of change has a rule naming the role that may decide it. The
defaults:

- `create_table`, `add_column` and `create_index`: a member;
- `drop_table` and `drop_column`, restoring and purging dropped objects, changing table
  ownership or grants: an admin.

Admins can tighten the rules. An approval node names its own role. Notifications go only to
those who can decide.

**Audit log.** `aios.audit_log` in the store is append-only: the app's role may insert but never
update or delete. It records:

- configuration changes (agents, skills, workflows, triggers, connectors);
- approvals and their decisions;
- archive and unarchive;
- role and membership changes;
- each `write_data` call, summarised (table, operation, row count, run).

Admins read it on the Organisation page, and agents can query it like any other table.

#### D9. Insights

The Insights page is built from the store: runs, steps, statuses, tool calls and token usage,
with costs from a per-model price table. Standard panels show:

- runs over time and their success rate;
- cost by agent and by workflow;
- the most used tools;
- the slowest workflows;
- how long approvals wait.

People can also ask for a chart in words. A built-in **insights agent** with `describe_data` and
`query_data` answers with a SQL query and a Vega-Lite spec. The query runs through the existing
`query_data` path: read-only role, read-only transaction, row and byte caps, statement timeout.
The spec is validated before it's drawn. Saved charts (`aios.insight_charts`) keep the SQL and
the spec, and run again as they're viewed, under the same constraints.

#### D10. The organisation assistant

The documents assistant becomes the **organisation assistant** (`builtin = 'assistant'`). Its
tools are `search` (documents, chats, run history and sources), `describe_data` and
`query_data`, and its instructions cover all of them.

It's made when its organisation is, not when the first chat is. An existing documents assistant
gets a new version with these tools. Versions already pinned are unchanged. A new organisation's
assistant can answer from everything in its store from the first day.

#### D11. Docs and code by domain

- **README.** Becomes an overview and a quick start. The rest moves to `docs/`, one page per
  domain:
  - accounts and organisations
  - data stores
  - agents and skills
  - tools
  - workflows
  - chats
  - triggers
  - sources
  - governance
  - deploy
  - tracing
- **Glossary and decisions.** `docs/glossary.md` defines the terms these pages use
  (organisation, store, run, turn, invocation, brief, source, connector, trigger, ...). Decisions
  go in `docs/decisions/`, starting with this one.
- **Stale lines.** Fix them as the pages move:
  - "one per user" for data stores (they're per organisation);
  - the reference to `plan/user-databases/plan.md` "on the plan branch";
  - "and later a mention in a chat" (mentions use the firing machinery already);
  - "once runs can pause" (they can).
- **Split modules along D1's boundary.** `models.py` becomes a package: platform models apart
  from store models, grouped by domain. Split these the same way:
  - `api/agents.py`
  - `api/chats.py`
  - `workflows.py`
  - `datastores/schema_changes.py`
- **One full-stack end-to-end test.** Compose with the scripted model (`tests/fake_model.py`),
  driven by Playwright against the real API. A member mentions an agent, the agent requests a
  schema change, an admin approves it, the run resumes, and its reply is posted in the chat.
  It runs in CI alongside the fake-API browser tests.

#### D12. Usage budgets

**What exists today.** aios measures usage but enforces no budget on it. What it has are
guards against runaway work and resource exhaustion:

| Kind | Mechanism | Default |
| --- | --- | --- |
| Concurrency | `SCHEDULER_MAX_CONCURRENT_RUNS`: schedules and chat agent turns fired at once, per API process. Excess waits, queued | 5 |
| Concurrency | Celery worker processes for workflow runs and ingests | per deployment |
| Time | `WORKFLOW_RUN_TIME_LIMIT`, a whole workflow run (waiting excluded) | 2 hours |
| Time | `WORKFLOW_RUN_NO_PROGRESS_LIMIT` | 5 minutes |
| Time | `INGEST_TIME_LIMIT` per attempt | 30 minutes |
| Time | the handoff call | 1 minute |
| Time | sandbox idle and total, and a cap per `run_python` call | 15 minutes, 2 hours |
| Time | the store roles' statement timeout | 30 seconds |
| Per tool | `web_search` `max_uses` per model request (each search is billed) | tool setting |
| Per tool | document and history search `max_results` | tool setting |
| Per tool | `query_data` rows and bytes | 200 rows, 64 KB |
| Per tool | `write_data` rows | 100 |
| Storage | store data and files, per store | 1 GiB, 5 GiB |
| Storage | one upload | 50 MB |
| Context | handoff window | 30 messages or about 8,000 tokens |
| Rate | sign-in, registration and password checks only | per email, IP or user |
| Chat | message length, mentions per message | 100,000 characters, 100 |

Measurement:
- Each agent run records its model input and output tokens and its tool calls.
- Workflow runs and the run pages total them.
- Langfuse traces carry the organisation and the starter.

Nothing turns these counts into money, adds them up per organisation or member, or stops a run
because of them. aios also sets no cap of its own on model calls or tokens within a turn: a
turn is bounded only by LangGraph's default recursion limit and the provider's output limit.

**What viewers mentioning changes.** Any member can already start unbounded spend by
mentioning. Letting viewers post and mention widens that to people who can't edit anything,
often a larger and less familiar group. The plan's other decisions make each mention cost
more:

- **Fan-out:** one message may mention 100 agents and workflows. Each costs a handoff call and a
  run, and a workflow runs every agent node in it, for up to two hours.
- **Amplification:**
  - `start_workflow` (D7) lets one run start others.
  - Event triggers (D3) on `chat.message` mean a viewer's ordinary message, with no mention at
    all, can start runs.
  - Handback (D4) adds a call per result.
  - Continuing threads (D5) make each turn's input larger, until compaction trims it.
- **Fairness:** the concurrency cap is per process, not per organisation. One organisation's
  burst queues behind it every other organisation's chat turns and schedules on that process.
  This problem exists today, and more mentions make it worse.
- **Acting with an agent's powers:** a viewer can't edit an agent but can mention one. The agent
  then acts with its own tools on the viewer's behalf: `write_data`, requesting schema changes,
  posting to Slack, commenting on GitHub. This is a privilege question as much as a cost one.
  Schema changes are safe, because a member or admin still decides them (D8). Direct writes and
  external actions aren't.

**Decision.** Add budgets in five layers, checked in this order before anything costs money:
invocation permission, then rate limits, then budget, then the handoff call.

1. **Measure as cost.**
   - Every billable unit goes into a usage ledger in the store, `aios.usage`: model calls
     (including handoff, handback and compaction), embeddings, web searches and sandbox
     seconds.
   - Each entry records its organisation, agent or workflow, run, the member who caused it, and
     the cause (manual, mention, schedule, event, `start_workflow`).
   - Each entry is priced from D9's price table.
   - Insights (D9) reports it.
2. **Cap each run.**
   - Set aios's own limits on model calls and tokens per turn, and a cost cap per run and per
     workflow run.
   - An agent or workflow can lower these, and only admins can raise them.
   - A turn that hits a cap ends with a clear error rather than being cut off silently.
3. **Budgets for the organisation and its people.**
   - An admin sets a monthly spend budget for the organisation. Admins are notified at 80%.
   - At 100%, new runs are refused while runs in flight finish. The refusal shows on the run card
     or schedule, and costs no handoff call.
   - Optional allowances per role and per member, with a lower default for viewers. They're
     counted against the person who caused the run, including runs their messages fire through
     `chat.message` triggers.
4. **Limits on invoking.**
   - A rate limit on mentions per member per minute (counted in Redis, as the sign-in limits
     are).
   - The cap on agents and workflows mentioned per message drops from 100 to 5.
   - Each agent and workflow has an **invocable by** setting: everyone, members, or admins.
     It defaults to **members** when the agent has any tool that writes or acts outside aios:
     `write_data`, connector actions such as `post_to_slack`, `start_workflow` or `post_message`
     to another chat. Otherwise it defaults to **everyone**, viewers included.
   - A refused mention says why on its card.
5. **Share capacity fairly.** The firing machinery takes each organisation's in-flight count into
   account: a share per organisation, coordinated through Redis across processes. One
   organisation's burst then waits on its own share rather than on everyone's.

Measurement, per-run caps, mention limits and **invocable by** land with D8, before viewers
exist and before event triggers. Organisation budgets and fair sharing land before D3.

## Sequencing

Each phase depends on the one before it. Phases 3 and 4 can run in parallel.

0. **Refactor and guard rails:**
   - R2 (archive)
   - R3 (many organisations)
   - D11's docs and module split
   - the full-stack end-to-end test

   The split comes first because nearly every later change touches `models.py` and the large
   API modules. The test guards the moves that follow.
1. **Foundation:** D1 (with R1 as its first step, and the pooler before reads switch), then
   D8 with the first parts of D12: measurement, per-run caps, mention limits and
   **invocable by**. Roles are needed by approvals, ownership and notifications. The audit log
   needs D1. Viewers can't mention until D12's guards exist.
2. **Team experience:** D6, D10 and D4.
3. **Agents and automation:** D5, D7, the rest of D12 (organisation budgets and fair sharing),
   then D3.
4. **Inflow and insight:** D2 and D9.

Each item gets its own plan in `plan/<item>/plan.md` before it starts, as earlier features did,
and each lands behind the existing migration discipline: store migrations expand now and
contract in a later release.

## Consequences

- An organisation's store is its whole record: it can be exported, backed up, restored or
  moved to another instance on its own, and its agents can query all of it.
- The platform database shrinks to identity, tenancy and coordination. Cross-database
  transactions are avoided by keeping each operation on one side of the boundary. Where both
  sides change (membership and `aios.people`, notifications), the platform side is the source
  and the store copy catches up.
- Connections per store grow, so the pooler becomes a required part of the deployment, locally
  and on AWS.
- The run history export, the agent hard delete flow and the empty-organisation rules are
  deleted, with their tests: roughly 1,700 lines of backend code and over 2,000 lines of tests.
- Moving the organisation into the URL touches every API route and frontend route once. It's
  done in phase 0, before more routes are added.
- Per-agent database roles and connector tools add security surface. Each needs an adversarial
  test before it ships: escaping an agent's role, and a connector reaching hosts or data beyond
  its grant.

## Open questions

- The default per-run caps and viewer allowance. These are set from measured usage once
  D12's ledger has a few weeks of data.

## Future

- **Value-based pruning.** Beyond pruning by age, a periodic semantic scan over chats and runs
  could judge how much context each holds: decisions, facts the team relies on, things agents
  still refer to, as against chatter and routine runs. It would offer admins targeted pruning
  options, such as keeping the decision and dropping the discussion around it, or summarising a
  thread before it's pruned. It builds on the search index and the usage ledger, and would
  itself be a budgeted run.
