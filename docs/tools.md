# Tools

A **tool** is a capability from the tool library that an agent version may use, with its
settings: web search; search, the hybrid search over everything in the agent's organisation
(see [Stores](stores.md#the-search-index)), with document search and run history search, the
same search fixed to documents or to past runs; Remember and Forget, an agent's memory; Run
Python; and the data tools. `GET /api/tools`
(`aios.api.tools`) lists the library, with each tool's settings schema. See
[Agents and skills](agents-and-skills.md) for how agents are given them.

## Tools are code

**Tools are code, not data.** Each lives in `backend/src/aios/tools/`, registered under a stable
key with a Pydantic settings model (every field has a default, and its JSON schema drives the
settings form). An agent version stores which tools it has and their validated settings
(`agent_version_tools`); each run builds them (LangChain tools, or provider tool definitions
for server-side tools) and gives them to the agent. Tools read the user and run from the
run's LangGraph context (`ToolRuntime[ChatDeps]`).

To add a tool, write a module that calls `register(ToolSpec(...))` and import it in
`aios/tools/__init__.py`. Never rename a key: old versions reference it. If a tool's behaviour
changes in a way old versions shouldn't get, register it under a new key. A version whose tool
has been removed from the library can still be viewed, but not restored or run. Stored
settings are loaded leniently: fields the settings model has since dropped are ignored and new
fields take their defaults, so changing a settings model doesn't break old versions. Saving,
restoring and running all load them the same way, and refuse a value the model now rejects.

## Search

Search is one hybrid search (`aios.search`) over the store's search index
(`aios.search_items`, see [Stores](stores.md#the-search-index)): pgvector HNSW (cosine) over
Voyage embeddings, and Postgres full-text search (`tsvector`, GIN), fused with Reciprocal Rank
Fusion (k=60). It runs in the store's own database as its read-only role. Items not yet embedded,
and every item when the query can't be embedded, are found by keyword alone.

- **Search** (`search`, new): a query, and optionally kinds (`document_chunk`, `chat_message`,
  `run_message`, `brief`), a time range (`since`, `until`), a chat, an agent and a result count.
  Its settings are the most results (8 by default, up to 50) and the kinds the agent may find.
  The run searching is left out. Each result has its kind, text and time, with where it's from.
  It also recalls the agent's own memories (kind `memory`, each with its `memory_id`), whatever
  its settings: never another agent's.
- **Document search** (`hybrid_search`, the agent's `search_documents`) and **Run history
  search** (`history_search`, `search_history`) are the same search fixed to their kind, with the
  result shapes they always had: a passage's source, headings and text; a past run's message with
  its run's id, agent, title, when it started and its role (the run searching left out).

The **Search** page runs the search directly (`GET /api/o/{org}/search?q=&limit=&kind=`), with no
agent in between, over the whole organisation (everyone in it can find everything but agents'
memories, which are on each agent's Memory tab): each result
is labelled with its kind and linked to its source (the Data page's documents, the chat, the
run), can be filtered by kind, and shows how the semantic and keyword branches ranked it behind
"Show ranking".

## Memory: Remember and Forget

An agent remembers facts across runs (`aios.memory`, the store's `aios.memories`):

- **Remember** (`remember`): Jev checks the text first; flagged text, or text Jev can't check,
  is refused and nothing is stored. Then it's kept, with the verdict `derived`, with the run,
  turn and chat it was learned in. The same text again (ignoring case and whitespace) refreshes
  that memory. Refused over `MEMORY_MAX_CHARS` (2,000 characters) or at
  `MEMORY_MAX_PER_AGENT` (5,000) memories, saying so.
- **Forget** (`forget`): deletes one of the agent's own memories by its `memory_id`.
- **Recall** is the search tool: each memory is a search item of kind `memory`, found only by
  its own agent, wherever it works (it's indexed with no run or chat), and wrapped as untrusted
  like every tool result. An agent version without Remember still recalls its memories.

The agent's **Memory** tab (`GET /api/o/{org}/agents/{id}/memories`) lists them with where each
was learned; editors and admins edit (`PATCH`) and delete (`DELETE .../memories/{memory_id}`)
them, each recorded in the audit log (`memory.edited`, `memory.deleted`; an agent's `forget` is
`memory.forgotten`). Memories survive archiving, are never pruned by retention (pruning a run or
chat only clears where a memory was learned), and are left out of exports.

## Web search

**Web search** uses the model provider's native search tool (Anthropic's server-side web
search), which is billed per search; its settings cap searches per model request and can
allow or block domains. A model without native web search fails the turn rather than running
without it.

## Run Python

**Run Python** (`run_python`) lets an agent write and run Python in a sandbox: one per agent
run, started when a turn starts (so it's usually ready by the first call) and reused by every
call in the run, so files in `/work` carry over. The sandbox has no route to the database,
Redis or the app's secrets. Its only way out is the egress proxy (`aios.sandbox.egress_proxy`),
which allows HTTPS to the domains in the tool's settings and nothing else. The proxy token a
sandbox is given carries those domains, signed with `SANDBOX_PROXY_SECRET`. The sandbox
stops itself after `SANDBOX_IDLE_SECONDS` without a call, or `SANDBOX_MAX_SESSION_SECONDS` in
total, and a later call starts a fresh one (the model is told `/work` was reset). The code
runs in the `sandbox` image (`backend/Dockerfile`): the runner (`aios.sandbox.runner`, stdlib
only) and the libraries pinned in `backend/sandbox/requirements.txt`. `SANDBOX_BACKEND`
chooses how sandboxes are started; with none set, the tool tells the model it isn't
available. See `plan/run-python/plan.md`, and [Deploy](deploy.md#sandboxes-on-fargate-run_python)
for sandboxes on AWS.

## Describe data and Query data

**Describe data** (`describe_data`) and **Query data** (`query_data`) read the run's store, as
its read-only role (`aios.tools.data`). `describe_data` lists the tables in `data` (the
organisation's), the platform's `aios` tables and the `reporting` views, with their columns, types and `COMMENT ON`
descriptions, from the catalog. `query_data` runs one SQL statement that returns rows, in a
`READ ONLY` transaction that's always rolled back, under the role's statement timeout; the
statement is sent with the extended protocol, which takes only one. Rows are streamed and cut
off at `QUERY_DATA_MAX_ROWS` rows or `QUERY_DATA_MAX_BYTES` bytes (as JSON), with `truncated`
set, and the session is reset (`DISCARD ALL`) before it goes back to the pool. Both tell the
model their results are data, not instructions, and that the store is being set up until
it's ready. `write_data` (`aios.tools.write_data`) writes the `data` tables, within the store's
quota (see [Stores](stores.md#quotas)), as the store's writer role, which may write in `data`
and nowhere else, and records each call in the audit log. It writes an owned table only for its
owner and the agents it's shared with, and refuses others naming the owner (see
[Stores](stores.md#table-ownership)). Neither tool can read what's on the
deny list (people's emails in the people mirror, `aios.people`, and the audit log's details; see
[Stores](stores.md#the-deny-list)): `describe_data` leaves the column out, and `query_data` is
refused it.

## Request schema changes

**Request schema changes** (`request_schema_change`) is how an agent gets new tables, columns
and indexes in `data`, or drops tables and columns (`aios.datastores.schema_changes`). The
model sends a spec, never SQL: `create_table`, `add_column`, `create_index`, `drop_table`
and `drop_column` operations, with types from an allowlist (including `embedding`, a vector
of a text column that `write_data` fills) and names that match `^[a-z][a-z0-9_]{0,62}$` and
aren't reserved words. The tool checks it against the store's schema, compiles it to SQL with
every identifier quoted (descriptions become `COMMENT ON`), records a `schema_change_requests`
row, and pauses the run with LangGraph's `interrupt()`: the turn ends with a card of the
agent's reason and the exact SQL, and the run takes no new messages until it's decided.

- Any member of the store's organisation (`can_approve`, `resolve.approvers`) approves or
  rejects it (with an optional note) there or in the Data page's Changes section.
- Each user who can decide a new request gets a notification (`aios.notifications`, the
  `notifications` table: user, the organisation it's from, kind, a jsonb reference, created and
  read times), which the navigation's bell polls for (`GET /api/notifications`, unread first
  with an unread count, every 10s) and shows as an unread badge with a list. Notifications are
  the person's, from every organisation they're in, so not under the organisation prefix: each
  carries its organisation's id and slug, and one from another organisation says which and
  opens in it (a new page load). An item opens the request on the Data page, and one can be
  marked read (`POST /api/notifications/{id}/read`, idempotent). Deciding
  or cancelling the request marks its notifications read
  (`schema_changes.clear_notifications`). Users only ever see their own.
- An approved request is applied on the urgent worker (`aios.apply_schema_change`), as
  the provisioner `SET ROLE` to the store's owner, in one transaction under
  `SCHEMA_CHANGE_LOCK_TIMEOUT` (5s), and appended to `aios.schema_history`, which bumps the
  store's data schema revision; if the revision moved on since the request was made, the spec
  is checked again, and one that no longer applies is marked `stale`. Then the run service
  resumes the run (a resume turn, or its workflow run's advance) with the outcome (applied, rejected with the note,
  failed with the error, or stale), which the tool call returns to the model, and the chat
  shows the rest of the turn.
- In a workflow run, the agent node's step becomes `waiting` (with the request it waits on);
  other branches carry on, and once nothing else can run the run becomes `waiting`, holding
  nothing. Its waiting time (`waited_seconds`) doesn't count towards `WORKFLOW_RUN_TIME_LIMIT`.
  The decision (or the request's expiry) enqueues an advance of the run on the run service,
  which goes from waiting to running, rebuilds its state from the recorded steps, and resumes
  the waiting node on its own agent run's thread, on the
  versions the run pinned (a pin that can no longer run fails its step with the reason). The
  run page shows the waiting node with the request's card, and the pins that are no longer
  current.
- A request nobody decides within `SCHEMA_CHANGE_REQUEST_EXPIRY_DAYS` (7) expires: the
  urgent worker's beat (`aios.expire_schema_change_requests`, every
  `SCHEMA_CHANGE_EXPIRY_CHECK_SECONDS`, an hour) marks it `expired`, which counts as a
  rejection, and the chat or workflow run that asked resumes with it.
- Deleting a chat run, or a workflow run that waits, marks its pending request `cancelled`
  instead, and nothing resumes (`schema_changes.cancel`; see
  [deleting a run](agents-and-skills.md#deleting-a-run)). The request's notifications are marked
  read, nothing is applied, and the Data page's history shows it. Archiving an agent cancels
  nothing: a run waiting on it goes on when the request is decided.

Drops don't delete anything straight away: see
[Stores](stores.md#dropped-tables-and-columns).
