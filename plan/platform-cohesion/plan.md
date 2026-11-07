# Platform cohesion: the phased plan

Status: **plan** (2026-11-07). The decisions are made in
[decision record 0001](../../docs/decisions/0001-organisation-database-as-system-of-record.md),
the words are the glossary's ([`CONTEXT.md`](../../CONTEXT.md)), and what waits for later is in
[the roadmap](../../docs/roadmap.md). This plan turns those decisions into ordered,
shippable steps. Nothing here is built yet.

Each step below is roughly one pull request, or a short series of them. Each one gets a detailed
plan of its own (`plan/<step>/plan.md`, as earlier features had) before work starts. That plan
settles the schema, API and screens. This document fixes the order, the scope of each step, and
when each step counts as done.

## Rules for every step

- **Migrations expand, then contract.** A platform or store migration must work with the code
  before it. Columns and tables are removed in a later release than the one that stops using
  them. Moving data between the platform database and the stores goes: dual-write, backfill,
  switch reads, contract.
- **Tool keys are never renamed.** `hybrid_search` and `history_search` stay registered. New
  behaviour gets new keys (`search`, `remember`, `post_message`, `start_workflow`, ...).
- **Every limit is a setting.** Caps, budgets, allowances, rate limits, in-flight caps, grace
  periods and retention all come from configuration, with generous defaults, overridable per
  organisation where 0001 says so. Real values come later (roadmap).
- **Words follow the glossary.** Screens and docs say organisation, chat, direct message, chat
  member, approval, turn, invocation. Code keeps its internal names where renaming would only
  churn: `data_stores`, `chat_members`, `organisation_members`.
- **Tests come with the change.** That means backend tests (`backend/tests`), the fake-API
  browser tests (`frontend/e2e`, updating `fake-api.ts`), and from step 0.3 the full-stack
  test.
- **Docs move with the code.** Once step 0.1 lands, each step updates its page in `docs/`, and
  the glossary if it adds or changes a term.

## Overview

| Phase | Steps | Depends on |
| --- | --- | --- |
| **0. Refactor and guard rails** | 0.1 docs split · 0.2 module split · 0.3 full-stack test · 0.4 sharded tests · 0.5 archive · 0.6 many organisations | — |
| **1. Foundation** | 1.1 pooler and turn lock · 1.2 store as system of record · 1.3 unified search, and removing the export · 1.4 retention · 1.5 roles and audit log · 1.6 invocations, usage and guards · 1.7 run executor and fair queue | phase 0 |
| **2. Organisation experience** | 2.1 chats and direct messages · 2.2 streaming, one chat experience · 2.3 organisation assistant and default agents · 2.4 delivery, handback and notifications · 2.5 export and import | phase 1 |
| **3. Agents and automation** | 3.1 continuing runs · 3.2 memory · 3.3 table ownership and per-agent roles · 3.4 structured workflows · 3.5 budgets · 3.6 event triggers · 3.7 lanes and autoscaling | phase 2 |
| **4. Inflow and insight** | 4.1 sources and connectors · 4.2 Slack, Google Drive, GitHub · 4.3 Insights | phase 3 (4.3 can start after 1.6) |

Phases 3 and 4 can run in parallel once phase 2 is done. Within each phase, the order below is
the order of dependency.

---

## Phase 0: Refactor and guard rails

The aim is a codebase that's easier to change, with a test that guards the core loop, and the
simplifications that every later step builds on.

### 0.1 Docs by domain

- **README.** Becomes an overview, the architecture diagram and a quick start. Everything else
  moves to `docs/`, one page per domain:
  - `accounts-and-organisations.md`
  - `stores.md`
  - `agents-and-skills.md`
  - `tools.md`
  - `workflows.md`
  - `chats.md`
  - `triggers.md`
  - `deploy.md`
  - `tracing.md`
  - an index page, `docs/README.md`.
- **Pages added later.** `sources.md`, `governance.md` and `insights.md` are created by the steps
  that build them.
- **Stale lines.** Fix them as they move:
  - "one per user" (stores are per organisation);
  - the plan "on the plan branch";
  - "and later a mention in a chat";
  - "once runs can pause".
- **Links.** The README links to `CONTEXT.md`, `docs/decisions/` and `docs/roadmap.md`.
- **Done when:** the README is under about 150 lines, every section of the old README has a home,
  and no page contradicts the code.

### 0.2 Modules by domain

These are pure moves, with no behaviour changes, so the later steps touch small files.

- **Models.** `aios/models.py` (1,275 lines) becomes a package, `aios/models/`. Platform models
  are separated from what will move to the store:
  - `identity.py`: users, organisations, members, invites;
  - `stores.py`: the `data_stores` registry;
  - `agents.py`
  - `workflows.py`
  - `triggers.py`
  - `chats.py`
  - `approvals.py`
  - `notifications.py`

  `aios.models` re-exports everything, so imports keep working.
- **Large modules.** Split these by sub-domain:
  - `api/agents.py` into CRUD, versions and runs;
  - `api/chats.py` into chats, members, messages and invocations;
  - `workflows.py` into validation, execution and steps;
  - `datastores/schema_changes.py` into spec, compile, apply and expiry.
- **Done when:** the whole backend suite and the browser tests pass unchanged, Alembic reports
  no difference (`alembic check`), and no module is over about 600 lines.

### 0.3 The full-stack test

- **Setup.** A Compose profile, `e2e`, runs the real API, workers, database and Redis, with the
  scripted model (`tests/fake_model.py`) as the model and no external calls. Playwright drives
  the real frontend against it.
- **First test: the core loop as it is today.**
  1. A member mentions an agent in a chat.
  2. The agent requests a schema change.
  3. A member approves it on the Data page.
  4. The run resumes, and the reply is posted in the chat.
- **CI.** A job beside the fake-API browser tests runs it.
- **Done when:** it passes in CI and fails if any link in the loop breaks. Later steps extend it
  rather than adding parallel harnesses.

### 0.4 Sharded test runs

The suites will grow with every step that follows, so CI should split them across machines
before they do. The aim is that CI time stays roughly flat as tests are added: add shards, not
waiting.

**Today:**
- one backend job runs lint, format, role setup, migrations, `alembic check` and all of `pytest`
  in sequence against one Postgres, one Redis and one RustFS;
- one frontend job runs the build, unit tests and every Playwright spec. Playwright runs specs in
  parallel on that machine (`fullyParallel`), but never across machines.

The backend tests share one database and write real rows. They also reset Redis between tests
and share one files bucket. That's why sharding across machines, each with its own services, is
the safe first move, rather than running in parallel inside one machine.

- **Backend shards.**
  - A CI matrix of N jobs. Each has its own Postgres, Redis and RustFS service containers, so
    shards can't see each other's data, rate limits or files.
  - Each job runs role setup and migrations, then its share of the tests.
  - Tests are split by file and balanced on recorded durations: `pytest-split`, with a
    `.test_durations` file in the repository, refreshed by a manual or weekly workflow.
  - The dependency is pinned exactly, as the dependency policy requires.
  - N is one number in the workflow.
- **Checks that run once.** Lint, format and `alembic check` move to their own fast job, so they
  aren't repeated per shard. A final job requires every shard to pass, so branch protection
  needs only one required check.
- **Order independence.**
  - Run the whole backend suite once in random order (`pytest-randomly`, pinned) and fix what
    fails: tests that rely on another's rows, or on global counts such as "every user" or "every
    store".
  - Keep random order on in CI, with the seed printed, so new order dependencies show up as soon
    as they're written.
- **Frontend shards.**
  - Playwright's own `--shard=i/N` across a matrix of jobs.
  - Each shard writes a blob report, and a final job merges them (`playwright merge-reports`)
    into one HTML report, uploaded on failure as today.
  - Unit tests (`pnpm test`) and the build run once, in their own job.
- **Full-stack test (step 0.3).** It uses the same Playwright sharding. Each shard brings up its
  own Compose stack, so shards share nothing.
- **Running locally.** `uv run pytest --splits N --group i` and `pnpm test:e2e --shard=i/N`
  reproduce one CI shard. The docs page on testing says how.
- **Not in scope.** Parallel workers inside one machine (`pytest-xdist`) would need a database,
  Redis index, bucket prefix and store name prefix per worker. Do it later only if a single
  shard becomes the bottleneck.
- **Done when:**
  - the backend and Playwright suites each run in N balanced shards;
  - the slowest shard takes close to total time divided by N, plus setup;
  - the suite passes in random order;
  - changing N is a one-line change;
  - a failing shard's report is still downloadable.

### 0.5 Archive instead of delete (R2)

- **Model.** Agents, skills and workflows get `archived_at` and `archived_by`. Names stay
  reserved while archived.
- **What archiving does:**
  - Nothing new starts on an archived agent or workflow: by hand, by schedule, by mention, as a
    workflow node, or as a workflow pinning it. Each refusal says it's archived.
  - Runs already going finish.
  - Schedules are paused.
  - It stays in chats, marked archived, and drops out of mention suggestions.
- **Remove the hard delete:**
  - `DELETE /api/agents/{id}` and its 409 dependents protocol;
  - the cancel and deactivate paths in `aios.dependents`;
  - `AgentDeleteDialog.tsx`;
  - the related tests.

  Keep a read-only "used by" list (live workflows, schedules and chats) on the agent's page. The
  same goes for workflows and skills.
- **Archive dialog.** Lists the data tables the agent will leave read-only. This becomes real in
  step 3.3; until then it lists nothing.
- **Unarchive.** Restores everything except the schedules, which stay paused until someone
  resumes them.
- **List filters.** Agents, skills and workflows lists get an Archived filter.
- **Done when:** no route deletes an agent, skill or workflow, archived ones can't start
  anything anywhere, and the full-stack test still passes.

### 0.6 Many organisations per person (R3)

- **Memberships.**
  - Drop the unique index on `organisation_members.user_id`.
  - Signing up makes no organisation and no store. Remove that path from
    `aios.auth.start_session`.
  - A person in no organisation lands on a page that creates one or explains how to join one.
  - Creating an organisation provisions its store.
- **Invitations.**
  - Accepting simply adds a membership.
  - Remove the empty-organisation rules from `organisations.py`: the empty check, deleting the
    personal organisation, and listing what blocks the join.
  - Remove `test_empty_organisation.py`.
- **Leaving.**
  - Removes the membership, and creates no personal organisation.
  - The last admin can't leave while others remain.
  - The last person can only delete the organisation.
  - Until step 1.5, "admin" means the creator.
- **The organisation in the URL.**
  - API routes move under `/api/o/{organisation_id}/...`.
  - SPA routes move under `/o/<slug>/...`, so organisations need a unique `slug`.
  - `CurrentOrganisation` reads the organisation from the path and checks membership. It stays
    the only place this is resolved.
  - `GET /api/me` lists the person's organisations.
  - The shell gets an organisation switcher, and old URLs redirect to the person's last-used
    organisation.
- **Deleting an organisation.**
  - An admin schedules it, with a grace period: `ORGANISATION_DELETION_GRACE_DAYS`, 14 by
    default.
  - Members are notified, and an admin can cancel during the grace period.
  - Afterwards the store is torn down through the existing `deleting` path, and the platform
    database keeps a record of the deletion.
- **Labels.** "Organisation" everywhere on screen.
- **Done when:**
  - a person can belong to two organisations and work in both in two tabs;
  - nothing makes a personal organisation;
  - deletion runs end to end with cancellation;
  - the browser tests cover the switcher and the onboarding page.

**Phase 0 is done when:** steps 0.1–0.6 are merged, the README is short, and the full-stack test
runs in CI, sharded like the rest of the suite.

---

## Phase 1: Foundation

The aim is that the store is the system of record, with roles, an audit log, usage tracking and
guards in place, and every run executes on a fair, shared executor pool.

### 1.1 Connection pooler and turn lock

- **Pooler.** Add a pooler in front of the stores: PgBouncer in Compose, RDS Proxy (or PgBouncer
  on ECS) in Terraform. Stores are reached through it in transaction pooling mode.
- **Turn lock.** Transaction pooling breaks session advisory locks, so the per-run turn lock in
  `checkpoints.py` moves to a row lock on the run, held for the turn, or to a Redis lock.
- **Connection limits.** Revisit `DATA_STORE_CONNECTION_LIMIT` and the per-process pools, now
  that the pooler multiplexes.
- **Done when:** the whole suite and the full-stack test pass through the pooler, two tabs'
  turns on one run still serialise, and the deploy docs cover the pooler.

### 1.2 The store as system of record (D1)

This moves, domain by domain, everything an organisation makes, says and produces into its
store. Each domain goes through the same four sub-steps, in its own release:

1. **Expand:** a store migration adds the tables.
2. **Dual-write and backfill:** writes go to both databases, and `migrate-all` copies existing
   rows, keeping their ids.
3. **Switch reads:** reads come from the store.
4. **Contract:** a later release drops the platform tables.

Domains, in order:

1. **People mirror.** `aios.people` in each store: member id, name and role. The platform
   updates it on membership, role and profile changes. Nothing reads it yet, but later steps
   need it.
2. **Agents, skills, workflows** and all their versions, nodes, edges and tool settings.
3. **Runs:** agent runs, workflow runs, steps, sandbox sessions, and the LangGraph checkpoints.
   Each store gets a `langgraph` schema and its own checkpointer.
4. **Triggers.** Definitions move to the store. The platform keeps a thin "due" index (trigger
   id, store, next run time) for the scheduler.
5. **Chats:** chats, chat members, messages, mentions, invocations and briefs.
6. **Approvals:** schema change requests, which become approvals in step 3.4. Notifications stay
   in the platform database, as references.

**Cross-database rules.** No transaction spans both databases. Where both sides change
(membership and `aios.people`), the platform writes first and the store catches up,
idempotently.

**Done when:**
- every domain reads from the store;
- the platform database holds only identity, organisations, memberships, invitations, the store
  registry and its credentials, notifications, and coordination indexes;
- the full-stack test passes with no organisation content in the platform database.

### 1.3 Unified search, then removing the export (D1, R1)

- **Index.** Add `aios.search_items` to the store. Each item records its kind, a reference to
  its row, its text, a `tsvector` and an embedding, when it was written, and its visibility (the
  organisation, or one direct message). Each writer indexes in the same transaction, and
  embedding happens afterwards under a budget. Backfill documents' chunks, chat messages, run
  messages and briefs.
- **Tool.** A new `search` tool, with filters for kind, time, chat, agent and source, enforces
  visibility from the run's context. `hybrid_search` and `history_search` keep their keys and
  become `search` fixed to their kinds. The Search page queries the same index, showing what the
  signed-in person may read.
- **Remove the run history export (R1).** This happens only once `history_search` reads the new
  index. Remove:
  - `datastores/history.py` and the `history_exports` outbox;
  - the `export_history` and `set_up_history_export` tasks and their beat entries;
  - the `history` branch of the store tree, with a migration that drops the `history` schema;
  - the `history_*`, `export_run_history` and `embed_run_history` columns;
  - `RunHistoryExport.tsx`, its API, and `test_history_export.py`.
- **Done when:** one search finds a document, a chat message and a past run's message; an agent
  version using `history_search` still runs; and the export code is gone.

### 1.4 Retention

- **Settings.** Two admin settings on the organisation, `run_retention_days` and
  `chat_retention_days`. Both default to "keep indefinitely", and the chat setting covers direct
  messages.
- **Daily pruning job, on the maintenance worker:**
  - **Runs** that finished before the cutoff, with their steps, checkpoints and index entries.
    Never runs still going or waiting on an approval. An agent's continuing run (from step 3.1)
    is pruned turn by turn.
  - **Messages** before the cutoff, with their mentions, invocations, briefs, handbacks and index
    entries. The chat itself stays, and a message whose invocation is still going is kept.
  - **Never pruned:** memory, data tables, documents and the audit log. Each prune is recorded
    in the audit log (from step 1.5).
- **Done when:** both settings appear on the organisation's Settings tab, and tests cover the
  edges (waiting runs, invocations still going, continuing runs).

### 1.5 Roles, approval rules and the audit log (D8)

- **Roles.**
  - `organisation_members.role` takes `admin`, `editor` or `participant`. Creators become
    admins, everyone else becomes an editor, and the creator stopgap goes.
  - One permission check per action, following 0001's D8 table, enforced in the API and
    reflected in the UI by hiding or disabling what isn't allowed.
  - Admins change roles on the Members tab.
  - The participant role exists from this step, but participants can't mention until step 1.6
    lands.
- **Approval rules.** A table of change kinds and the role that may decide each:
  - an editor decides `create_table`, `add_column` and `create_index`;
  - an admin decides drops, restores, purges, and table ownership or grant changes.

  Admins can tighten the rules. Approval notifications go only to people who can decide.
- **Audit log.** `aios.audit_log` in the store, append-only: the app's roles may insert but
  never update or delete. It records:
  - configuration changes;
  - approvals and their decisions;
  - archive and unarchive;
  - role and membership changes;
  - each `write_data` call, summarised;
  - prunes, renames, imports and organisation deletion scheduling.

  Admins see it on the organisation page.
- **Done when:** each role's allowed and refused actions are covered by tests, the audit log
  can't be altered by the app's roles (with a test proving it), and the browser tests cover a
  participant's view.

### 1.6 Invocations for every run, usage and guards (D4, D12)

- **Invocations.** Every run has an invocation, whatever its cause (mention, schedule, event, by
  hand, `start_workflow`). It records who or what caused it, where its result goes, and its
  causation chain. Extend `chat_invocations` (now in the store) and backfill causes for existing
  runs.
- **Usage ledger.** `aios.usage` in the store records every model call (including handoff),
  embedding, web search and sandbox second. Each entry carries its organisation, agent or
  workflow, run, turn, person, cause, and an estimated cost from a price table kept in
  configuration.
- **Guards**, each a setting with a generous default:
  - limits on model calls, tokens and cost per turn and per workflow run. A turn that hits one
    ends with a clear error.
  - mentions per message: 5 by default, down from 100;
  - a rate limit on mentions per person, counted in Redis;
  - an **invocable by** setting on each agent and workflow: everyone (the default), editors or
    admins.

  Checks run in this order: permission, rate limit, budget, then the handoff. A refusal shows on
  the card and costs no model call.
- **Done when:** every billable call writes exactly one ledger row (with a test that counts
  them), each guard refuses at its limit, and participants can mention.

### 1.7 Run executor and fair queue (D13, core)

- **Queue.** A table in the platform database records each item's organisation, store, lane,
  readiness time, attempts and lease. Executors claim with `FOR UPDATE SKIP LOCKED`, picking
  the organisation with the least in flight among those under their cap
  (`ORGANISATION_MAX_IN_FLIGHT`). Redis pub/sub wakes them, so they don't poll.
- **Executor service.** A new ECS service and Compose service, using the API image. Each process
  runs many turns at once on asyncio, up to a setting (50 by default). It runs every agent turn,
  whatever caused it. The API stops running turns: remove that role from `aios.firing` and
  `launch_turns`, and keep the scheduler only to enqueue what's due.
- **Workflows advance node by node.**
  - Each ready node is a queue item. Finishing one runs a short coordinator step that records the
    step, works out which nodes are now ready, and enqueues them. This reuses the rebuilding of a
    run's state from its steps that resume already does.
  - `WORKFLOW_RUN_TIME_LIMIT` becomes an elapsed-time check at each step.
  - The `workflows` Celery queue and worker are removed.
- **Shared model rate limit.** A token bucket per model in Redis, shared by every executor and
  divided by the same fairness rule.
- **Graceful deploys.** On shutdown, an executor stops claiming, finishes what it can within a
  grace period, and re-queues the rest from its last checkpoint.
- **Done when:**
  - a long workflow holds no process while it waits on the model;
  - one organisation flooding the queue doesn't delay another's chat turn past a set target,
    with a test for it;
  - a deploy mid-turn resumes the turn instead of failing it;
  - the full-stack test passes on executors.

**Phase 1 is done when:** steps 1.1–1.7 are merged and contracted, the platform database holds no
organisation content, and participants can safely mention.

---

## Phase 2: Organisation experience

The aim is one streamed chat experience with the right rules, results that come back to people,
and an assistant that knows everything in the store.

### 2.1 Chats and direct messages (D6)

- **Remove owners and private chats.**
  - Remove `ChatMemberRole` and the owner checks.
  - Today's private chats become direct messages, and today's owners become ordinary chat
    members.
  - Remove the hand-over logic in `organisations.py` for when an owner leaves.
- **Chats** are open: anyone in the organisation can find, join and leave one. Any chat member
  can rename or describe it and add or remove agents and workflows. Only admins remove people
  and archive or unarchive chats. An archived chat is read-only but searchable.
- **Direct messages** are closed:
  - they exist only for their chat members;
  - anyone in one can add people, agents and workflows, who see its history from the start;
  - leaving is the only way out, and nobody removes anyone;
  - a direct message never becomes a chat, though copying it into a new chat is offered;
  - admins can't read them;
  - search, memory and triggers respect them.
- **UI.** The Chats list separates chats and direct messages, with a "New direct message" flow.
  The Members tab lists chat members by kind.
- **Done when:** the permission table in 0001's D6 is covered by tests, private chats have
  migrated cleanly, and an admin can't read a direct message through any route or search.

### 2.2 Streaming, and one chat experience (D6)

- **Stream.** `GET /api/o/{organisation}/chats/{id}/stream` is a server-sent events stream
  carrying new messages, invocation updates and reply token deltas. Executors publish to Redis;
  API processes relay to browsers. A reply appears as a draft message that fills in as it
  streams.
- **Catching up.** `?after=N` stays for after a reconnect. The one-second poll and the 30-second
  hidden poll go. Unread counts update from the stream.
- **One experience.** Remove the one-to-one agent chat on the run page (`useChat` in
  `pages/Run.tsx`). Run pages become read-only: trace, events, usage and versions per turn. A
  conversation with an agent is a direct message. "Chat with this agent" opens or creates one.
- **Done when:** replies stream in chats and direct messages, a dropped connection recovers
  without gaps or duplicates, and nothing polls every second.

### 2.3 Organisation assistant and default agents (D10)

- **Assistant.** `builtin = 'assistant'`, made when the organisation is. Its tools are `search`,
  `describe_data` and `query_data`. Existing documents assistants get a new version with these
  tools.
- **Default chat agents.** Admins choose which agents join every new chat; the organisation
  assistant is the default. Direct messages get only the agents chosen when they're made.
- **Done when:** a new organisation's assistant can answer from documents, chats, past runs and
  data tables, and the defaults setting applies to new chats only.

### 2.4 Delivery, handback and notifications (D4)

- **`post_message` tool.** An agent posts to a chat or direct message it's a chat member of.
  Mentions in what it posts start nothing.
- **Deliver to.** Triggers can deliver to any chat, and to a direct message only if whoever set
  them up is in it. If they leave, deliveries stop and the trigger says why.
- **Handback.**
  - Summarises:
    - workflow outputs;
    - trigger deliveries;
    - `start_workflow` results posted later;
    - any long or JSON output.
  - Uses the handoff model, is traced and tagged `handback`, and counts in the usage ledger.
  - Keeps the full output behind a disclosure on the run card.
  - Posts the raw output with a note if it fails.
  - Doesn't summarise an agent's reply to a mention.
- **Notifications.** New kinds:
  - `run_failed`: to whoever started the run, or for a trigger's run, its creator and the
    admins;
  - `reply`: to whoever mentioned;
  - `mention`: to a person mentioned;
  - `approval_needed`: to whoever can decide.

  The bell lists them across all the person's organisations.
- **Done when:** a scheduled workflow's summary lands in a chat, every notification kind is
  covered, and the full-stack test checks a handback.

### 2.5 Export and import (D14)

- **Export.** An editor exports an agent or workflow version to a file: its configuration, the
  skills it uses, and nested workflows.
- **Import.** Creates new, unconnected copies, renaming them on a name clash. Connector tools are
  carried by name and leave the version unrunnable until they're set up. Imports go in the audit
  log. Data, memory, runs and credentials never travel.
- **Done when:** an export from one organisation imports and runs in another, and a version that
  needs a missing connector says so.

**Phase 2 is done when:** steps 2.1–2.5 are merged, and the full-stack test covers a direct
message, a streamed reply and a handback.

---

## Phase 3: Agents and automation

The aim is agents that persist and own their data, structured workflows, and automation that
reacts to events, all under budgets.

### 3.1 Continuing runs (D5)

- **One run per conversation.** An agent keeps one run per chat or direct message, and each
  mention is a new turn.
- **Current version.** The run takes the agent's current version at each turn, and each turn
  records its version. Workflow runs, and runs waiting on an approval, stay pinned.
- **Brief.** The handoff brief becomes "what has happened since your last turn".
- **Compaction.** Past a token threshold, the older turns are summarised into the model's view by
  the handoff model, and the latest turns stay word for word. Full turns stay in the store and
  the index until retention prunes them.
- **Done when:** an edit to an agent changes its next turn in an existing chat, and a very long
  conversation keeps working, with compaction recorded in the ledger.

### 3.2 Memory (D5)

- **Table.** `aios.agent_memory`, with `remember` and `forget` tools. Recall is through `search`
  with kind `memory`, only the agent's own.
- **Visibility.** Each memory takes the visibility of where it was learned: a memory from a
  direct message is recalled only there.
- **Memory tab.** On the agent's page, people can see, edit and delete memories, each person
  seeing only those from where they could see.
- **Done when:** a memory from a direct message never surfaces in a chat (with a test for it), and
  memories survive archiving and unarchiving.

### 3.3 Table ownership and per-agent database roles (D5)

- **Ownership.** `aios.table_ownership` records which agent owns each table. A schema change
  makes the requesting agent the owner. Existing tables get an owner from their request's agent,
  or none.
- **Who may write.** Only the owner, and agents it's shared with. Editors and admins share and
  transfer tables on the Data page, recorded in the audit log. A table whose owner is archived is
  read-only.
- **Per-agent roles.** Each agent gets its own database login role in the store, granted exactly
  what ownership and sharing give it. Its data tools connect as that role through the pooler.
  The detailed plan settles the mechanism under one constraint: SQL the model writes must not be
  able to escape the role. `SET ROLE` on a shared login isn't enough.
- **Escape test.** An adversarial test tries `RESET ROLE`, `set_config('role', ...)`,
  `SET SESSION AUTHORIZATION`, and reading or writing another agent's table.
- **Done when:** the escape test passes, sharing and transfer work, and archiving an owner makes
  its tables read-only.

### 3.4 Structured workflows and approvals (D7)

- **Typed inputs and outputs.**
  - Workflows and their nodes may declare input and output JSON Schemas. An agent node with an
    output schema uses structured output and is checked against it.
  - A node's input is a template over its predecessors' fields.
  - A run's input is checked against the version's input schema.
  - The editor gets schema and template fields.
- **Conditional edges.**
  - JSONLogic conditions on a source node's output.
  - A node starts when each predecessor has finished (succeeded or skipped) and at least one
    incoming edge is active. A node with no active incoming edge is skipped, and skipping
    propagates.
  - Graphs without conditions behave as before.
- **Approvals.**
  - Schema change requests become one kind of **approval**. The other kind is a new `approval`
    node, which names the role that decides it.
  - Both kinds share the waiting, expiry, notification and resume code.
  - The Data page's Changes tab becomes an **Approvals** list that covers both kinds.
  - Approvers see the reason in full, and an agent asking for an approval is told so.
- **`start_workflow` tool.**
  - Its settings name the workflows an agent may start.
  - It waits up to a set time for the output; past that, the result is delivered by handback to
    the conversation the turn came from.
  - The causation chain caps the depth and refuses cycles.
- **Done when:** a workflow branches on structured output, pauses at an approval node, and
  resumes on the decision, and the full-stack test covers an approval node.

### 3.5 Budgets (D12)

- **Monthly organisation budget.** Admins set it. Admins are notified at 80%. At 100%, new
  invocations are refused, costing no model call, and runs already going finish.
- **Allowances.** Optional per role and per person, charged to whoever caused the invocation,
  including through `chat.message` triggers.
- **Done when:** a budget refusal shows on the card and on the trigger, and allowances are
  charged to the right person.

### 3.6 Event triggers (D3)

- **Trigger kind.** `TriggerKind` gains `event`. The events are:
  - `document.ingested`
  - `table.written`
  - `schema_change.applied`
  - `chat.message`
  - `webhook.received`
- **Event outbox.** `aios.events` in the store, written in the same transaction as the change
  that causes the event. A flag on the store in the platform database means the dispatcher
  never scans idle stores. The dispatcher enqueues matching triggers on the executor queue.
- **Filters and safeguards:**
  - each event has its filters, as in D3;
  - debounce windows;
  - loop prevention through the causation chain;
  - webhooks get their own URL and an HMAC secret, and their payloads are checked against the
    workflow's input schema.
- **Direct messages.** `chat.message` triggers can listen to a direct message only if whoever set
  them up is in it.
- **Done when:** each event kind fires its trigger once, a loop is refused at its cap, and a burst
  is coalesced.

### 3.7 Lanes and autoscaling (D13)

- **Lanes:**
  - **interactive**, with reserved capacity, claimed first;
  - **background**;
  - **ingest and sync**, on workers sized for them;
  - **urgent maintenance**: approvals, applying changes, restores;
  - **routine maintenance**: migrations, measuring, retention, expiry and sync cleanup.

  The beat only enqueues.
- **Autoscaling.** Executors and ingest workers scale on queue depth and the age of the oldest
  item, published as metrics, with a small floor. The target is interactive latency.
- **When to do it.** As load needs it. It can start any time after step 1.7.
- **Done when:** a batch of schedules doesn't delay a chat reply past target, and idle capacity
  scales down to the floor.

**Phase 3 is done when:** steps 3.1–3.6 are merged, and step 3.7 is in place or scheduled against
measured load.

---

## Phase 4: Inflow and insight

The aim is context arriving from the tools teams already use, and the organisation seeing how
its agents perform.

### 4.1 Sources and connectors (D2)

- **Sources.** A source model in the store, of these kinds:
  - upload: today's documents;
  - URL, re-fetched on a schedule;
  - webhook, shared with step 3.6;
  - email inbox;
  - connector.
- **Connectors:**
  - credentials are encrypted in the platform database, never in the store;
  - an MCP client handles the connection, with native adapters only where MCP can't do bulk sync;
  - sync runs on the `sources` lane, incrementally from a stored cursor, and reconciles deletions
    against what the source still lists;
  - synced items are indexed as `source_item` for search;
  - a `connector` tool kind exposes a connector's tools to agent versions, which pin the tools
    allowed.
- **Lifecycle.**
  - Uploaded documents stay until someone deletes them, even when the same file also syncs.
  - Synced items follow their source.
  - Removing a connector deletes what it synced, unless the admin keeps it as documents.
- **Admin only.** Admins add and remove sources and connectors. Picking restricted content warns
  that everyone in the organisation will be able to search it.
- **Done when:** a URL source and a test connector sync, update and reconcile deletions, and a
  connector tool runs in an agent turn.

### 4.2 Slack, Google Drive, GitHub

- **Slack:**
  - syncs channels the admin picks, as messages with their threads;
  - a `post_to_slack` tool posts to allowed channels.
- **Google Drive:**
  - syncs folders the admin picks;
  - files go through the existing ingest pipeline as synced documents.
- **GitHub:**
  - syncs repositories the admin picks: issues, pull requests and comments, and optionally docs
    files;
  - tools comment on and open issues.
- **Each one ships with:**
  - OAuth setup;
  - a sync health indicator on the Data page;
  - tests against recorded API responses.
- **Done when:** each connector syncs, stays current and posts or comments through its tool.

### 4.3 Insights (D9)

- **Standard panels** come from the store and the usage ledger, showing whatever is recorded:
  - turns and workflow runs over time and their success rate, by agent, workflow and cause;
  - tokens, tool calls and estimated cost, by agent, workflow and person;
  - the most used tools and agents;
  - the slowest workflows and nodes, and where runs fail;
  - approval wait times and approval rates;
  - chat activity;
  - storage against its caps;
  - sync health.

  Runs from before their counts were kept show as "not tracked".
- **Charts you ask for in words.**
  - A built-in insights agent writes SQL and a Vega-Lite spec. The SQL runs through the existing
    `query_data` path (read-only role, read-only transaction, caps), and the spec is validated
    before drawing.
  - Saved charts (`aios.insight_charts`) run again on view under the same limits.
- **Navigation.** Replaces the "Coming soon" Insights page.
- **When to do it.** The panels can ship as soon as step 1.6's ledger exists. Prompted charts
  follow.
- **Done when:** every panel reads from real data, and a prompted chart can be saved and reloaded.

**Phase 4 is done when:** steps 4.1–4.3 are merged and documented.

---

## Risks to watch

| Risk | Where | Mitigation |
| --- | --- | --- |
| Data loss while moving domains into stores | 1.2 | Dual-write, backfill, verify counts per store, switch reads, then contract a release later. Keep ids, and rehearse on a copy of production. |
| Connection exhaustion as all traffic moves to stores | 1.1, 1.2 | The pooler first; idle per-store engines evicted. |
| Model-written SQL escaping an agent's role | 3.3 | Separate login roles, plus the adversarial test as a merge gate. |
| Direct message content leaking | 1.3, 2.1, 3.2 | One visibility rule shared by search, memory and triggers, with tests from each side. |
| Runaway cost before budgets | 1.6, 3.5 | Per-turn caps, mention limits and **invocable by** before participants can mention. |
| One organisation starving others | 1.7 | Fair claiming with per-organisation caps, plus a starvation test. |
| Scope creep in connectors | 4.1, 4.2 | MCP first, three connectors only, everything else on the roadmap. |

## Tracking

| Step | Plan | Status |
| --- | --- | --- |
| 0.1 Docs by domain | — | not started |
| 0.2 Modules by domain | — | not started |
| 0.3 Full-stack test | — | not started |
| 0.4 Sharded tests | — | not started |
| 0.5 Archive | — | not started |
| 0.6 Many organisations | — | not started |
| 1.1 Pooler and turn lock | — | not started |
| 1.2 Store as system of record | — | not started |
| 1.3 Unified search, remove export | — | not started |
| 1.4 Retention | — | not started |
| 1.5 Roles and audit log | — | not started |
| 1.6 Invocations, usage and guards | — | not started |
| 1.7 Executor and fair queue | — | not started |
| 2.1 Chats and direct messages | — | not started |
| 2.2 Streaming, one chat experience | — | not started |
| 2.3 Organisation assistant | — | not started |
| 2.4 Delivery, handback, notifications | — | not started |
| 2.5 Export and import | — | not started |
| 3.1 Continuing runs | — | not started |
| 3.2 Memory | — | not started |
| 3.3 Table ownership, per-agent roles | — | not started |
| 3.4 Structured workflows and approvals | — | not started |
| 3.5 Budgets | — | not started |
| 3.6 Event triggers | — | not started |
| 3.7 Lanes and autoscaling | — | not started |
| 4.1 Sources and connectors | — | not started |
| 4.2 Slack, Google Drive, GitHub | — | not started |
| 4.3 Insights | — | not started |
