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
new run. The handoff brief becomes "what has happened in the chat since your last turn". Long
threads are compacted by summarising older turns into the thread.

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
| Post in chats, mention, start runs | ✓ | ✓ | |
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
   D8. Roles are needed by approvals, ownership and notifications. The audit log needs D1.
2. **Team experience:** D6, D10 and D4.
3. **Agents and automation:** D5, D7 and D3.
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

- Whether viewers may post in chats (read-only observers, or participants without edit rights).
- Retention: how long runs, checkpoints and synced items are kept, and whether an organisation
  can choose.
- Whether an agent's run per chat (D5) is compacted by summarising or by windowing alone.
- Which connectors to build first. A proposal: Slack, Google Drive and GitHub.
