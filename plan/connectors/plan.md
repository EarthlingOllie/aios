# Connectors: a builder that compiles a data request into a scheduled Python script

Status: **draft plan** (2026-10-20, revised after the second review). Nothing here is built. The
[Decisions](#decisions) are settled; the questions in
[Architectural ambiguities](#architectural-ambiguities) still need answers. It picks up the
first data-ingestion discussion (`plan/data-ingestion/plan.md` on
`claude/agent-data-ingestion-pbarab`), most of whose prerequisites have since been built: data
stores, `query_data`, `write_data`, `request_schema_change`, `run_python`, paused workflow runs,
notifications and the scheduler.

## Goal

A member says what data they want ("daily FX rates for GBP", "every new FCA policy statement as
a PDF"). A builder works like a compiler. It produces a Python script that a worker then runs on
a schedule, with no model in the loop:

1. **Find sources.** Search for ways to get the data (APIs, feeds, pages, files), and probe them.
2. **Ask the member.** Present the options and pause until the member approves one or sends
   feedback. Feedback goes back into the next proposal.
3. **Choose a target.** Decide whether the data is structured or unstructured:
   - structured data goes to tables in the organisation's data store, through a schema change
     request;
   - unstructured data goes to S3, as documents in a new folder of the store.
4. **Write and test the script** in the sandbox `run_python` uses, against the real source.
5. **Choose a frequency** from how often the source changes, and get approval for the connector
   as a whole: the script, the domains it reaches, its target and its schedule.
6. **Schedule it.** The scheduler runs the approved script periodically, in a sandbox, and the
   platform stores what it produced.

The model's work is all at build time. A scheduled run is deterministic: it's the approved
script, run again. This keeps the "compiler, not runtime" decision from the first discussion. It
drops that discussion's proposal of a fixed library of declarative source types in favour of
generated code, which the sandbox now makes safe to run.

What the builder produces is a **connector**: an approved, versioned script, with its domains,
target and schedule (§2).

## Decisions

| # | Question | Decision |
| --- | --- | --- |
| D1 | What is the builder? | **A built-in workflow with fixed steps.** Each step is an agent node, and the order of the steps is fixed by the graph, not chosen by a model. Loops (feedback, then a revised proposal; a failing test, then a fix) happen inside a step's agent run, so the graph stays acyclic. See §6. |
| D2 | How does a workflow pause for a member? | **The `request_approval` tool**, available to any agent. Called in a workflow's agent node, it pauses that node, and the run waits once nothing else can run, as schema change requests already do. |
| D3 | Generalise the pause mechanism, or copy it? | **Generalise it**: a registry of request kinds, with waiting steps pointing at a request by kind and id (§1). |
| D4 | Repeat loads | **Every load is idempotent.** A structured target must declare an upsert key, which the builder sets in its schema change; documents are deduplicated by `external_id`, or by hash when there's none. Re-running a connector never duplicates data. Batches are written in transactions, with one transaction per run where the output fits. |
| D5 | Credentials for sources | **v1 supports keyless sources only.** Next comes organisation secrets: encrypted like data store passwords, entered on a page (never in chat, never seen by a model), named in a connector version, and given as environment variables to that connector's sandbox only. |
| D6 | Approval gates | **Three gates, and any member may approve**, as for schema changes: the source, the schema change (or the new folder), and the final connector. Every new connector version, edits included, goes through the final gate again. |
| D7 | Drift and failures | **v1 notifies members**, and a member starts a repair build by hand: the builder workflow, given the current version and the failing run. Automatic repair comes later; its result would still go through approval. |
| D8 | Build-time egress | **The approved source grants its domains to the builder's workflow run** (`run_domain_grants`). Each node's sandbox may reach its `run_python` settings' domains plus the run's grants, minted into its proxy token. Until a source is approved, the scout probes only through `web_search`; to probe a candidate directly, it asks for that domain with `request_approval` first. No wildcard allowlists. |
| D9 | A final rejection that needs an earlier step | **v1**: publish can only revise the schedule and the summary; a rejection that needs a different source or target fails the run, and the member starts a new build with the note, reusing the draft's still-valid parts. **Later**: a "rewind to step" action on the workflow run page, which re-runs a node and those after it on the same draft, for every workflow. No cycles. |
| D10 | Steps that need no model | **v1**: saving the connector and its schedule happens in the publish step's `save_connector` tool, built from the draft. A **code node** kind for workflows waits until a second built-in workflow needs a deterministic step. |
| D11 | Passing results between steps | **A connector draft**: `connector_drafts`, one row per builder run, with typed fields written by the steps' tools and rendered into each node's input. `save_connector` builds the version from the draft, never from the model's arguments. |
| D12 | Can the organisation edit the built-in workflow? | **Yes**, like any workflow, as the documents assistant can be edited. The gates are enforced by the tools, not the graph: `save_connector` refuses a draft with no approved source or no target, so an edited graph can't skip an approval. |
| D13 | Prompt injection | **Wrap untrusted content in tags**, so models treat it as data and not instructions (§8), on top of the approval gates. Every card shows the exact artefact (the full script, diffed against the previous version; the exact SQL), not the model's summary of it. |
| D14 | Approvals nobody answers | **They expire after 7 days**, as schema change requests do. Expired counts as rejected, and the agent is told so. |
| D15 | Is it its own entity, and what's it called? | **Its own entity, called a connector**: `connectors`, `connector_versions`, `connector_runs` (§2). It's a different runtime from an agent (no model, no thread) with different limits, and shares the versioning helpers, not the tables. |
| D16 | Who owns runs and costs? | **As schedules do**: a scheduled run has `created_by` null and `trigger_id` set. `CONNECTOR_MAX_RUNS_PER_DAY` per organisation guards cost until billing exists. |
| D17 | What is a "directory" in S3? | **Real folders**: a store table `aios.document_folders`, `documents.folder_id` and `documents.external_id`, shown on the Data page and found by document search. A folder is the entity the documents describe, and each run writes under its own prefix: `stores/<store>/folders/<entity>/<run id>/<document id>` (§3). |

## What the codebase already gives us

- **Pausing for a member** (`request_schema_change`): the tool records a request, then calls
  LangGraph's `interrupt()`. The turn ends with a card, members get notifications, and a decision
  resumes the run with the outcome as the tool's result. In a workflow, the node's step becomes
  `waiting`, and the run becomes `waiting` once nothing else can run, holding no worker. Requests
  expire after 7 days. **But all of this is specific to schema change requests:**
  `workflow_run_steps.waiting_on_request_id` is a foreign key to `schema_change_requests`, and
  `workflows.py` and `api/agents.py` look for `REQUEST_KEY` in the interrupts.
- **Structured writes**: `request_schema_change` (a spec, not SQL; approved, then applied on the
  provisioning worker) and `write_data` (catalog-checked inserts and upserts as `_rw`, with
  embeddings, quota and row caps).
- **Unstructured storage**: documents in S3 at `stores/<store>/documents/<id>`, with a
  `documents` row in the store (filename, size, SHA-256, `uploaded_by`) and the Docling →
  chunks → Voyage ingest that makes them searchable. Documents have no folders, no source and no
  external id.
- **The sandbox** (`aios.sandbox`): backends (dev, Fargate), an in-sandbox runner that returns
  only exit code, stdout and stderr (about 20 KB each), and egress only to signed allowlisted
  domains through the proxy. It has no route to Postgres, Redis, S3 or AWS credentials. A
  session is keyed to an **agent run** (`sandbox_sessions.agent_run_id`), and its allowlist is
  the agent version's `run_python` settings, fixed when the version is saved.
- **Scheduling** (`aios.scheduler`, `triggers`): cron expressions in a timezone, claimed with
  `SKIP LOCKED`, targeting an agent (a turn in the API process) or a workflow (a Celery task on
  the `workflows` queue). `TriggerTargetKind` has those two values only.
- **Built-in agents**: `agents.builtin` (today only `'documents'`, the documents assistant),
  with one live agent of each kind per organisation. Workflows have no built-in kind yet.
- **Workflows** run agent nodes in a fixed DAG, passing each node's final reply as text along
  its edges. There are no cycles, no conditional edges and no node kinds other than agent and
  workflow.
- **Versioning**: identity row plus immutable versions, compared by hash, with runs pinned to a
  version. Agents, skills and workflows all work this way.

## Proposed design

### 1. A general `request_approval` tool (the "pause and wait" tool)

A new tool lets any agent ask the members a question and wait for the answer:

- Arguments: `title`, `proposal` (markdown, shown to the member), optional `options` (a list of
  choices for the member to pick from), and `reason`.
- The member **approves** (picking an option, if any), **rejects**, or **replies with
  feedback** (free text). The tool returns `{decision, option, feedback, decided_by}`.
- Feedback isn't a rejection. The agent reads it, revises, and calls the tool again with a new
  proposal. Each round is its own request, so the history shows how the proposal changed.
- It reuses the schema change machinery as-is: a request row, `interrupt()`, a notification per
  member, a card in the chat or on the run page, expiry, and resuming in a chat or a workflow.

This means **generalising the waiting machinery**, not copying it. Waiting becomes "waits on a
request of some kind":

- New table `approval_requests`, with the same shape as `schema_change_requests` minus the
  spec and SQL. Add `kind`, `title`, `proposal`, `options`, `decision`, `option` and
  `feedback`.
- Replace `workflow_run_steps.waiting_on_request_id` with `waiting_on_kind` plus
  `waiting_on_id` (or one nullable foreign key per kind, guarded by a check constraint, as
  `triggers` does).
- Replace the `REQUEST_KEY` lookups in `workflows.py` and `api/agents.py` with a registry of
  request kinds. Each kind supplies `find`, `outcome`, `cancel` and `clear_notifications`.
- Add a notification kind `approval_request`, and on the Data page (or a new Approvals page) a
  list of pending approvals alongside the schema changes.

This is useful without connectors, so it goes first.

### 2. Connectors: what the builder compiles to

A **connector** belongs to the organisation and is versioned like an agent:

- `connectors`: the identity row (name, description, organisation, `created_by`,
  `updated_by`, soft delete).
- `connector_versions`: immutable. Each holds:
  - the script;
  - `allowed_domains`;
  - the target: `{kind: "tables", tables: [...]}` or `{kind: "documents", folder_id}`;
  - limits: timeout and output cap;
  - the build run that made it (`agent_run_id`) and the approval request that approved it.

  A version runs only once it's approved. Editing the script makes a new version, which needs
  approving again.
- `connector_runs`: one per execution. It records:
  - the version it ran;
  - its cause (schedule, by hand, or a test);
  - status, timings and heartbeat;
  - `rows_written` per table and `documents_written`;
  - a tail of stdout and stderr, and the error.

  A Connectors page lists them, as the Runs tabs do.
- `connector_state`: a small JSON blob per connector that the platform keeps between runs. It's
  the script's cursor or watermark for incremental pulls (see the output contract).

### 3. The output contract: how a sandboxed script stores data

The sandbox can't reach the database or S3, and that is deliberate (see
`plan/run-python/plan.md`). So the script doesn't store anything itself. It **writes its
output to files**, and the platform, as trusted code, loads them:

```
/in/state.json                      the cursor the previous run left ({} on the first run)
/out/records/<table>.jsonl          one JSON object per row, for a structured target
/out/documents/<name>               files, for an unstructured target
/out/documents.jsonl                optional: per-file {name, external_id, title, source_url, meta}
/out/state.json                     the cursor for the next run
```

After the script exits 0, the **loader** (a Celery task on a new `connectors` queue) does the
following:

- **Records**: it fetches the records and writes them through `write_data`'s planner, as
  upserts on the table's declared upsert key (D4). That gives the same catalog checks, type
  coercion, embeddings, quota and row caps. A run writes in one transaction where its output
  fits, and in batches otherwise; since every write is an upsert, a run that fails partway is
  safely run again.
- **Documents**: each file goes under its folder and the run that fetched it,
  `stores/<store>/folders/<entity>/<run id>/<document id>`. `<entity>` is the folder's slug,
  named by the builder after what the documents describe (`fca-policy-statements`), and the
  key is built only from the platform's ids, never from the script's file names (§7). The
  loader upserts the `documents` row by `(folder_id, external_id)`: a file whose SHA-256
  hasn't changed is skipped, and a changed one is written under the new run and its row moved
  to it (what happens to the old object is A3). Then it queues the normal ingest. The file's
  own name and its `meta` are kept on the row.
- **State**: it saves `/out/state.json` only if the load succeeded, so a failed run is retried
  from the same cursor.

The runner needs a way to hand the files back: a `GET /files` endpoint that streams `/out` as a
tar, capped at `CONNECTOR_MAX_OUTPUT_BYTES`, plus `PUT /files` to seed `/in`. The builder's
`run_python` sandbox uses the same contract, so a test run of the script runs it exactly as the
scheduler will.

### 4. Running a connector

- A new `connectors` Celery queue and worker run `aios.run_connector(run_id)`:
  1. claim the run;
  2. start a **fresh sandbox per run**, with the version's `allowed_domains` and secrets (D5);
  3. seed `/in`, execute the script with the version's timeout, and fetch `/out`;
  4. stop the sandbox, load the output, and record the outcome.
- Sandbox sessions stop being keyed only to agent runs. A session gets an owner kind
  (`agent_run` or `connector_run`), or the connector worker drives a backend directly with no
  session row, since a connector run makes only one call.
- Limits come from the version, under deployment ceilings: `CONNECTOR_MAX_TIMEOUT_SECONDS` (more
  than `run_python`'s 300), `CONNECTOR_MAX_CONCURRENT_RUNS` per process, and at most one running
  run per connector (a new run is skipped, not queued, while one is still running).
- A failure notifies the organisation's members (notification kind `connector_failed`), with
  stderr's tail on the run page. Three failures in a row pause the schedule.

### 5. Scheduling

- Add `TriggerTargetKind.connector` and `triggers.connector_id`, and extend the target check
  constraint. Firing creates a queued `connector_run` and enqueues it, as a workflow schedule
  does. It runs the connector's **current approved version**.
- The builder proposes the cron expression and the timezone. The final approval card shows them
  next to a preview of the next runs (the existing schedule preview). A deployment floor,
  `CONNECTOR_MIN_INTERVAL_MINUTES`, refuses anything more frequent.
- The Scheduler page lists connector schedules with the others, and a connector's page has a
  Schedule tab, as agents and workflows do.

### 6. The builder: a built-in workflow

The builder is a **built-in workflow** (D1), made for the organisation on demand, as the
documents assistant is (`workflows.builtin = 'connector_builder'`). Its graph is fixed, so the
steps always run in the same order. Each step is an agent node with its own skill and a small
set of tools, and each step that needs a member's say pauses the run with `request_approval`.

```
scout ──▶ target ──▶ author ──▶ publish
```

| Step | What it does | Tools | Pauses for |
| --- | --- | --- | --- |
| **scout** | Restates the request, searches for sources and probes them, then offers 1–3 options, each with its domains, auth needs, cost, freshness and notes on terms or licence. Revises on feedback. | `web_search`, `run_python`, `request_approval` | Choosing the source |
| **target** | Decides whether the data is structured or unstructured. Structured: reads the store's tables and requests new or changed tables, with an upsert key. Unstructured: creates the folder. | `describe_data`, `request_schema_change`, `create_document_folder` | The schema change (none for a folder: it's empty and harmless) |
| **author** | Writes the script to the output contract and runs it against the real source until `test_connector` passes. Saves the script to the draft. | `run_python`, `test_connector`, `describe_data` (not `query_data`: §7, R9) | Nothing |
| **publish** | Chooses a frequency from how often the source changes, then proposes the whole connector: a plain summary, the script, the domains, the target and the schedule with its next runs. Revises on feedback. Once approved, the connector version and its schedule are created, and optionally a first run. | `save_connector` (pauses through the approval machinery) | The final connector |

What the graph fixes, and what the agents decide:

- **Fixed**: the order of the steps, which tools each step has, and that each gate happens.
  A step can't skip ahead or call a tool from another step.
- **Decided by the agents**: which source to propose, structured or unstructured, the script,
  and the frequency. Every one of these reaches a member before it takes effect.
- **Loops stay inside a step.** Feedback on a proposal comes back as the result of the
  `request_approval` call, and the same agent run revises and asks again. A failing test
  is fixed by the same author run. The graph never needs a cycle.

**The draft.** Workflow edges pass only text, which is too loose for what later steps need
(the chosen source, the domains, the target tables or folder, the script). So the run keeps a
**connector draft**: one row per builder run (`connector_drafts`, keyed by `workflow_run_id`),
with typed fields each step's tools fill in. Approving the source writes it there, along with
its domains. `request_schema_change` and `create_document_folder` record the target.
`test_connector` records the last script that passed. Each node's input is the request, then the
draft rendered as text, so the agent sees everything decided so far. `save_connector` builds the
version from the draft and not from the model's arguments, so what's approved is what was tested.

**Domains across steps.** Each node is its own agent run, and so has its own sandbox, and `/work`
doesn't carry over from scout to author. The draft carries the script. Approving the source
grants its domains to the whole builder run, so the author's sandbox can reach them (D8).

**Starting a build.** A member starts one from a **New connector** form on the Connectors page.
It starts a run of the builder workflow with the request as its input, and opens the run's page,
where each waiting node shows its approval card. The builder can also be mentioned in a chat, as
any workflow can; its approval cards then show on the run card. A repair build (D7) is a run
with the current version and the failing run as its input, whose scout step starts from the
existing source.

### 7. Keeping each organisation's data siloed

Each organisation's data lives in two silos, and they're enforced differently:

- **The database**: each store is a Postgres database of its own, with its own roles.
  `store_session(id, role)` connects to that database as that store's role, which can't
  connect to any other. Postgres enforces this silo, **but the platform holds every store's
  credentials**, so code that opens the wrong store id reaches another organisation's data in
  full.
- **S3**: one bucket for every store, with each store's files under `stores/<store id>/`. The
  API's and ingest worker's IAM roles may get, put and delete anything under `stores/*`
  (`infra/terraform/data_store_files.tf`), so **this silo is enforced only by our code**
  building the key.

Today only the platform's own code writes into either silo, from inputs it controls (an upload,
a `write_data` call that's checked against the catalog). The loader is different. It writes
what **untrusted code** produced, on a schedule, with no member watching. The output contract
(A1, option a) keeps that code away from the silos, but the loader becomes the one place where
untrusted output meets credentials for every store. These are the risks:

| # | Risk | How it happens | Mitigation |
| --- | --- | --- | --- |
| R1 | **Writing to the wrong organisation's store** | The loader takes the store from somewhere other than the connector's organisation: the output, the draft, a stale id, or a bug. | Resolve the store in one place: run → connector → organisation → `data_store_for`. Record it on the run when it's created, and check it again at load. Never read it from the output. Test it with two organisations, as `test_organisation_scoping` does. |
| R2 | **S3 keys that escape the store's prefix** | File names from the script used in the key: `../../stores/<other>/…`, absolute paths, encoded slashes, Unicode look-alikes. | Build keys only from the platform's ids: `stores/<store>/folders/<entity>/<run id>/<document id>`. The script's name is kept only as metadata. Assert every key starts with the store's prefix. **And** scope each load at the IAM level: the loader assumes a role with a session policy limited to `stores/<store id>/*`, so a bug can't cross silos even if the check is wrong. |
| R3 | **A malicious output archive** | The tar `/files` returns holds symlinks, hard links, devices, absolute or `..` paths, a huge file, millions of tiny ones, or a compression bomb. | Never extract to disk. Stream the members, and accept only regular files at the contract's paths. Cap the member count, each file's size and the total. No compression, or decompression with a bounded output. |
| R4 | **Writing outside the approved target, inside the right store** | The output names a table the version didn't declare, or a column added since approval. | The loader accepts only the version's declared tables and folder. Anything else fails the run, which writes nothing. `write_data`'s catalog check refuses unknown or dropped columns. |
| R5 | **Overwriting rows someone else wrote** | A connector upserts into a table that agents or another connector also write. Its upsert key matches their rows. | v1: a connector writes only tables its own schema change created, recorded in a store table (`aios.table_owners`). Sharing a table between writers is a later decision (A4). |
| R6 | **The database role is wider than the target** | The loader writes as the store's `_rw` role, which may write every `data` table. Only R4's code check narrows it. | v1: R4's check. Later: a Postgres role per connector, granted only its tables, which the loader uses with `SET ROLE`. The provisioner already makes roles, so this is an extension of it. |
| R7 | **Filling the store, or running up costs** | A run loads up to the quota, which blocks every other write for the organisation. Or every row and document is embedded, which runs up the Voyage bill. | Per-run caps on rows, documents and bytes, under the store quota. Embedding at most a capped number of rows per run. The daily run cap (D16). The final approval shows the expected size per run. |
| R8 | **Poisoned data spreads to every agent** | A source's content is loaded into the store. Every agent with document search or `query_data` then reads it, so an injection travels from one source to the whole organisation. | Tags (D13, §8) on everything read back from the store. Provenance on every row and document (`connector_id`, `connector_run_id`), so search results say where they came from and a bad run can be deleted. |
| R9 | **Exfiltration at build time** | An agent with both `query_data` and `run_python` reads the store, then sends the data to an approved domain. | No builder step has both. The author step only gets `describe_data` (tables and columns, no rows). The scheduled script never sees the store: its only input is its own state. |
| R10 | **Shared sandboxes** | The dev backend runs every run, of every organisation, in one container. | It's for development only. Refuse `SANDBOX_BACKEND=dev` when the environment is production. On Fargate each run is its own microVM, and `connector_state` is only given to that connector's runs. |
| R11 | **The loader process is the weak point** | It parses untrusted output in a process that can decrypt every store's credentials. | Parse with `json` only (no pickle, YAML or eval). Keep the loader small. Parse and validate before opening any store session. Later, parse in a separate process that has no credentials, and pass only checked rows to the writer. |

For comparison, option (b), giving the sandbox scoped credentials, would need a network route
from the sandbox subnets to RDS and S3. The silos would then rest on credentials held by
untrusted code, which could send them to any approved domain. And a Postgres role, however
narrow, still lets that code send arbitrary SQL (heavy queries, catalog reads). So option (a)
is still the safer one, with R1–R11 as its cost.

### 8. Untrusted content in tags (D13)

Everything a model reads that came from outside the platform, or from data stored by agents
and connectors, is wrapped in a tag that says where it came from:

```
<untrusted_data source="run_python">
...stdout...
</untrusted_data>
```

- One helper in `aios.tools` does the wrapping. It escapes any `</untrusted_data` inside the
  content, so data can't close the tag early, and writes `source` itself.
- Wrapped: `run_python` output, `test_connector` samples and errors, `query_data` and
  `describe_data` results (column descriptions are model-written), document and run history
  search hits, and the draft's fields when they're rendered into a node's input (a source's
  description is the scout's summary of a page). The existing "data, not instructions" lines
  (`DATA_NOT_INSTRUCTIONS`) move into the helper.
- Not wrapped: members' messages and approval feedback, which are instructions by design.
- Every agent's instructions say once that content in these tags is data, and that
  instructions in it must not be followed.
- `web_search` results come back from Anthropic's server-side tool as their own content
  blocks, which the platform can't wrap. The model already sees them as search results, and
  the scout step holds no tools that write.
- Tags lower the risk without removing it. The approval gates (D6), with the exact artefact on
  each card, are still the control that matters.

## Architectural ambiguities

These still need decisions. Each has a recommendation.

| # | Question | Options | Recommendation |
| --- | --- | --- | --- |
| A1 | **Where does the data get written from?** The sandbox has no route to the database or S3, and each organisation's store and S3 prefix is a silo. | (a) the output contract plus a trusted loader (§3); (b) give the sandbox scoped credentials (a per-connector role with `INSERT` on its tables, presigned PUT URLs), which needs a network path from sandbox subnets to RDS and S3 | **(a)**, with the mitigations in §7. The loader is where untrusted output meets credentials for every store, so R1 (the wrong store), R2 (S3 keys, plus a session policy scoped to the store's prefix) and R3 (the archive) are built in phase 2, not later. |
| A2 | **Structured or unstructured, and records that are both** | See [A2 in more detail](#a2-in-more-detail) below | **One target per connector in v1**, chosen by the questions the data answers, with metadata kept on documents (option c below). Both targets at once later. |
| A3 | **What happens to a document's old object when it changes?** With a prefix per run, a changed document is written under the new run. | (a) delete the old object once the new one is loaded, so the store holds one copy; (b) keep it as history, counted against the files quota, until a retention period passes | **(a)** for v1, so the files quota measures what's current. (b) only if someone needs past versions of a source's documents. |
| A4 | **Can several writers share a table?** (R5) | (a) no: a connector writes only the tables it created, and agents can't `write_data` to them; (b) yes, with rows marked by writer (`connector_id`), and upserts that only match their own writer's rows | **(a) for v1**: simple, and safe against R5. (b) when two sources need to feed one table. |

### A2 in more detail

The target step chooses where the data goes. Each target answers different questions:

| | Tables (`data` schema) | Documents (a folder) |
| --- | --- | --- |
| Good for | Discrete fields you filter, sort, sum or join: prices, rates, counts, statuses, dates | Text you read, quote or search by meaning: PDFs, web pages, articles, long bodies |
| Read by | `query_data` (SQL); the Data page's Tables tab | Document search (hybrid, with citations); the Data page's Documents tab; downloads |
| Made by | A schema change request (columns, types, upsert key) | Docling parsing, chunking and embedding, as uploads are |
| Weak at | Long text: an `embedding` column can hold its vector, but **no tool searches embedding columns by meaning yet** (`query_data` can't embed a query) | Questions about fields: "how many were published in March" needs SQL, and the metadata isn't in columns |

Many sources are **mixed records**: fields plus a body of text. Some examples:

- **Support tickets**: id, status, priority and dates, plus the conversation.
- **Regulator publications**: reference, type, date and topic, plus a PDF.
- **News or blog posts**: title, author and date, plus the article.
- **Job adverts**: title, salary, location and closing date, plus the description.

Four ways to handle them:

- **(a) Pick one target.** The builder asks which questions the member will ask. Mostly
  "how many, which, when" means a table, with the text in a column. Mostly "what does it say"
  means documents.
- **(b) Both, linked by `external_id`.** Rows in a table and documents in a folder, written by
  one run. This is the fullest option, but it needs two approvals (the schema change and the
  folder). The two writes can't share a transaction (Postgres and S3), so the loader writes
  documents first, then rows, and a run that fails between them is put right by the next
  (D4 makes both idempotent). And deleting or retaining one side means doing the same on the
  other.
- **(c) Documents with typed metadata.** `documents.jsonl` already carries per-file `meta`.
  Keep it in a `documents.meta` jsonb column, which `query_data` can filter, because the
  store's `aios` tables are readable and `describe_data` lists them. This gives SQL over the
  fields without a schema change, though it's less tidy than real columns.
- **(d) Tables plus meaning search.** A tool that searches a table's embedding column by
  meaning would let tables hold the text well. It's useful beyond connectors, but it's a new
  tool.

Recommendation: **(a) with (c) for v1.** File-shaped sources go to documents with their fields
in `meta`, and field-shaped sources go to tables. That covers most mixed records with one
target. (b) and (d) come later, when a real source needs them. The contract already has both
output directories, so (b) is a loader change and not a redesign.

## Phases

Each phase is a mergeable PR (or a small stack), and each is useful on its own.

1. **Generic approval requests** (D2, D3). Request-kind registry, `approval_requests`, waiting by
   kind and id (migration of `waiting_on_request_id`), the `request_approval` tool, the card
   with Approve, Reject and Reply in chats and run pages, notifications, expiry, and the
   dependents and cancel paths. Tests mirror `test_schema_change_requests` and
   `test_workflow_pause`.
2. **Connectors and the runtime**:
   - `connectors`, `connector_versions`, `connector_runs` and `connector_state`;
   - the loader's silo checks (§7: R1–R4, with the IAM session policy) and the untrusted-content
     tags (§8);
   - runner `/files` endpoints;
   - sandbox sessions not tied to agent runs;
   - the `connectors` queue and worker;
   - the loader for records (on `write_data`'s planner);
   - a Connectors page with versions, runs and "Run now";
   - connectors can be created by hand with a pasted script, which tests the runtime without
     the builder.
3. **Document folders** (D17): store migration `0008` (`document_folders`, and on `documents`
   `folder_id`, `external_id`, `source_url`, `meta` and `connector_run_id`), the loader for
   documents, and folders on the Data page.
4. **Scheduling connectors**: `TriggerTargetKind.connector`, the scheduler path, the floor and
   the daily cap, failure notifications, and pausing after repeated failures.
5. **Builder tools**: `run_python` with the output contract and domain grants for a workflow run (D8),
   `create_document_folder`, `test_connector`, `save_connector`, and the connector draft (D11).
6. **The builder workflow**: built-in workflows (`workflows.builtin`), the four step agents
   and their skills, the draft shown on the run page, the New connector form, and an
   end-to-end test with the fake model (`tests/fake_model.py`) driving a build through all four
   steps and three approvals against a local HTTP fixture.
7. **Later**: organisation secrets (D5), repair builds and then auto-repair (D7), rewinding a
   workflow run to a step (D9), code nodes (D10), mixed targets (A2), and connector lineage on
   rows and documents (which run wrote what).

## Risks

- **Output size**: a source that returns gigabytes doesn't fit the output contract in one run.
  The builder must write incremental scripts (cursor in state), and the first run's backfill
  may take many runs. `test_connector` should flag a projected run size over the cap.
- **Sandbox start latency** on Fargate (tens of seconds) is fine for a schedule, but it sets a
  practical floor on frequency. Most sources don't need more than hourly.
- **Terms of use and robots**: the source-approval card should carry the builder's notes on
  licence and terms, so the member makes that call knowingly. The platform doesn't enforce it.
- **Generated code quality**: scripts are reviewed by members who may not read Python. The
  final card should lead with a plain summary (what it fetches, from where, how often, how
  much), and show the code below it.
