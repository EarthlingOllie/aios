# Ingestion pipelines: an agent that compiles a data request into a scheduled Python script

Status: **draft plan** (2026-10-20, revised after the first review). Nothing here is built. The
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
5. **Choose a frequency** from how often the source changes, and get approval for the pipeline
   as a whole: the script, the domains it reaches, its target and its schedule.
6. **Schedule it.** The scheduler runs the approved script periodically, in a sandbox, and the
   platform stores what it produced.

The model's work is all at build time. A scheduled run is deterministic: it's the approved
script, run again. This keeps the "compiler, not runtime" decision from the first discussion. It
drops that discussion's "declarative connectors first" proposal in favour of generated code,
which the sandbox now makes safe to run.

## Decisions

| # | Question | Decision |
| --- | --- | --- |
| D1 | What is the builder? | **A built-in workflow with fixed steps.** Each step is an agent node, and the order of the steps is fixed by the graph, not chosen by a model. Loops (feedback, then a revised proposal; a failing test, then a fix) happen inside a step's agent run, so the graph stays acyclic. See §6. |
| D2 | How does a workflow pause for a member? | **The `request_approval` tool**, available to any agent. Called in a workflow's agent node, it pauses that node, and the run waits once nothing else can run, as schema change requests already do. |
| D3 | Generalise the pause mechanism, or copy it? | **Generalise it**: a registry of request kinds, with waiting steps pointing at a request by kind and id (§1). |
| D4 | Repeat loads | **Every load is idempotent.** A structured target must declare an upsert key, which the builder sets in its schema change; documents are deduplicated by `external_id`, or by hash when there's none. Re-running a pipeline never duplicates data. Batches are written in transactions, with one transaction per run where the output fits. |
| D5 | Credentials for sources | **v1 supports keyless sources only.** Next comes organisation secrets: encrypted like data store passwords, entered on a page (never in chat, never seen by a model), named in a pipeline version, and given as environment variables to that pipeline's sandbox only. |
| D6 | Approval gates | **Three gates, and any member may approve**, as for schema changes: the source, the schema change (or the new folder), and the final pipeline. Every new pipeline version, edits included, goes through the final gate again. |
| D7 | Drift and failures | **v1 notifies members**, and a member starts a repair build by hand: the builder workflow, given the current version and the failing run. Automatic repair comes later; its result would still go through approval. |
| D8 | Build-time egress | **The approved source grants its domains to the builder's workflow run** (`run_domain_grants`). Each node's sandbox may reach its `run_python` settings' domains plus the run's grants, minted into its proxy token. Until a source is approved, the scout probes only through `web_search`; to probe a candidate directly, it asks for that domain with `request_approval` first. No wildcard allowlists. |
| D9 | A final rejection that needs an earlier step | **v1**: publish can only revise the schedule and the summary; a rejection that needs a different source or target fails the run, and the member starts a new build with the note, reusing the draft's still-valid parts. **Later**: a "rewind to step" action on the workflow run page, which re-runs a node and those after it on the same draft, for every workflow. No cycles. |
| D10 | Steps that need no model | **v1**: saving the pipeline and its schedule happens in the publish step's `save_pipeline` tool, built from the draft. A **code node** kind for workflows waits until a second built-in workflow needs a deterministic step. |
| D11 | Passing results between steps | **A pipeline draft**: `pipeline_drafts`, one row per builder run, with typed fields written by the steps' tools and rendered into each node's input. `save_pipeline` builds the version from the draft, never from the model's arguments. |
| D12 | Can the organisation edit the built-in workflow? | **Yes**, like any workflow, as the documents assistant can be edited. The gates are enforced by the tools, not the graph: `save_pipeline` refuses a draft with no approved source or no target, so an edited graph can't skip an approval. |

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

This is useful without pipelines, so it goes first.

### 2. Pipelines: what the builder compiles to

A **pipeline** belongs to the organisation and is versioned like an agent:

- `pipelines`: the identity row (name, description, organisation, `created_by`,
  `updated_by`, soft delete).
- `pipeline_versions`: immutable. Each holds:
  - the script;
  - `allowed_domains`;
  - the target: `{kind: "tables", tables: [...]}` or `{kind: "documents", folder_id}`;
  - limits: timeout and output cap;
  - the build run that made it (`agent_run_id`) and the approval request that approved it.

  A version runs only once it's approved. Editing the script makes a new version, which needs
  approving again.
- `pipeline_runs`: one per execution. It records:
  - the version it ran;
  - its cause (schedule, by hand, or a test);
  - status, timings and heartbeat;
  - `rows_written` per table and `documents_written`;
  - a tail of stdout and stderr, and the error.

  A Pipelines page lists them, as the Runs tabs do.
- `pipeline_state`: a small JSON blob per pipeline that the platform keeps between runs. It's
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

After the script exits 0, the **loader** (a Celery task on a new `pipelines` queue) does the
following:

- **Records**: it fetches the records and writes them through `write_data`'s planner, as
  upserts on the table's declared upsert key (D4). That gives the same catalog checks, type
  coercion, embeddings, quota and row caps. A run writes in one transaction where its output
  fits, and in batches otherwise; since every write is an upsert, a run that fails partway is
  safely run again.
- **Documents**: it puts each file at
  `stores/<store>/folders/<folder>/<external id or hash>`, upserts a `documents` row by
  `(folder_id, external_id)` (replacing the object when the SHA-256 changed), and queues the
  normal ingest.
- **State**: it saves `/out/state.json` only if the load succeeded, so a failed run is retried
  from the same cursor.

The runner needs a way to hand the files back: a `GET /files` endpoint that streams `/out` as a
tar, capped at `PIPELINE_MAX_OUTPUT_BYTES`, plus `PUT /files` to seed `/in`. The builder's
`run_python` sandbox uses the same contract, so a test run of the script runs it exactly as the
scheduler will.

### 4. Running a pipeline

- A new `pipelines` Celery queue and worker run `aios.run_pipeline(run_id)`:
  1. claim the run;
  2. start a **fresh sandbox per run**, with the version's `allowed_domains` and secrets (D5);
  3. seed `/in`, execute the script with the version's timeout, and fetch `/out`;
  4. stop the sandbox, load the output, and record the outcome.
- Sandbox sessions stop being keyed only to agent runs. A session gets an owner kind
  (`agent_run` or `pipeline_run`), or the pipeline worker drives a backend directly with no
  session row, since a pipeline run makes only one call.
- Limits come from the version, under deployment ceilings: `PIPELINE_MAX_TIMEOUT_SECONDS` (more
  than `run_python`'s 300), `PIPELINE_MAX_CONCURRENT_RUNS` per process, and at most one running
  run per pipeline (a new run is skipped, not queued, while one is still running).
- A failure notifies the organisation's members (notification kind `pipeline_failed`), with
  stderr's tail on the run page. Three failures in a row pause the schedule.

### 5. Scheduling

- Add `TriggerTargetKind.pipeline` and `triggers.pipeline_id`, and extend the target check
  constraint. Firing creates a queued `pipeline_run` and enqueues it, as a workflow schedule
  does. It runs the pipeline's **current approved version**.
- The builder proposes the cron expression and the timezone. The final approval card shows them
  next to a preview of the next runs (the existing schedule preview). A deployment floor,
  `PIPELINE_MIN_INTERVAL_MINUTES`, refuses anything more frequent.
- The Scheduler page lists pipeline schedules with the others, and a pipeline's page has a
  Schedule tab, as agents and workflows do.

### 6. The builder: a built-in workflow

The builder is a **built-in workflow** (D1), made for the organisation on demand, as the
documents assistant is (`workflows.builtin = 'pipeline_builder'`). Its graph is fixed, so the
steps always run in the same order. Each step is an agent node with its own skill and a small
set of tools, and each step that needs a member's say pauses the run with `request_approval`.

```
scout ──▶ target ──▶ author ──▶ publish
```

| Step | What it does | Tools | Pauses for |
| --- | --- | --- | --- |
| **scout** | Restates the request, searches for sources and probes them, then offers 1–3 options, each with its domains, auth needs, cost, freshness and notes on terms or licence. Revises on feedback. | `web_search`, `run_python`, `request_approval` | Choosing the source |
| **target** | Decides whether the data is structured or unstructured. Structured: reads the store's tables and requests new or changed tables, with an upsert key. Unstructured: creates the folder. | `describe_data`, `request_schema_change`, `create_document_folder` | The schema change (none for a folder: it's empty and harmless) |
| **author** | Writes the script to the output contract and runs it against the real source until `test_pipeline` passes. Saves the script to the draft. | `run_python`, `test_pipeline`, `query_data` | Nothing |
| **publish** | Chooses a frequency from how often the source changes, then proposes the whole pipeline: a plain summary, the script, the domains, the target and the schedule with its next runs. Revises on feedback. Once approved, the pipeline version and its schedule are created, and optionally a first run. | `save_pipeline` (pauses through the approval machinery) | The final pipeline |

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
**pipeline draft**: one row per builder run (`pipeline_drafts`, keyed by `workflow_run_id`),
with typed fields each step's tools fill in. Approving the source writes it there, along with
its domains. `request_schema_change` and `create_document_folder` record the target.
`test_pipeline` records the last script that passed. Each node's input is the request, then the
draft rendered as text, so the agent sees everything decided so far. `save_pipeline` builds the
version from the draft and not from the model's arguments, so what's approved is what was tested.

**Domains across steps.** Each node is its own agent run, and so has its own sandbox, and `/work`
doesn't carry over from scout to author. The draft carries the script. Approving the source
grants its domains to the whole builder run, so the author's sandbox can reach them (D8).

**Starting a build.** A member starts one from a **New pipeline** form on the Pipelines page.
It starts a run of the builder workflow with the request as its input, and opens the run's page,
where each waiting node shows its approval card. The builder can also be mentioned in a chat, as
any workflow can; its approval cards then show on the run card. A repair build (D7) is a run
with the current version and the failing run as its input, whose scout step starts from the
existing source.

## Architectural ambiguities

These still need decisions. Each has a recommendation.

| # | Question | Options | Recommendation |
| --- | --- | --- | --- |
| A1 | **Where does the data get written from?** The sandbox has no route to the database or S3. | (a) the output contract plus a trusted loader (§3); (b) give the sandbox scoped credentials (a per-pipeline role with `INSERT` on its tables, presigned PUT URLs), which needs a network path from sandbox subnets to RDS and S3 | **(a)**. It keeps the sandbox invariant, and reuses `write_data`'s checks, quota and embeddings. Costs: the output is bounded by the cap, and a very large backfill has to be paginated across runs through the state cursor. |
| A2 | **Structured or unstructured: who decides, and what about mixed records** (tickets, filings with metadata)? | (a) the target step decides one target, and the member confirms it in the schema change or in the final approval; (b) allow both, with rows in a table plus documents linked by `external_id` | **(a) for v1**, with the contract already allowing both directories, so (b) is only a loader change later. |
| A3 | **What is a "directory" in S3?** Documents are flat today. | (a) a store table `aios.document_folders` (name, description, `pipeline_id`), with `documents.folder_id` and `documents.external_id`, keys under `folders/<id>/`, and the Data page's Documents tab grouped by folder; (b) a bare S3 prefix with no rows | **(a)**. It's a store migration (`0008`). Ingested files then show on the Data page, count against the files quota, and are found by document search (which could filter by folder later). |
| A4 | **Who owns runs and costs?** A scheduled run has no member. | As schedules do: `created_by` null and `trigger_id` set. Quotas: the data store caps already bound storage; is a per-organisation cap on sandbox minutes needed? | Follow schedules. Add `PIPELINE_MAX_RUNS_PER_DAY` per organisation as a cheap guard until billing exists. |
| A5 | **Is a pipeline its own entity, or a target kind of agent?** | (a) new `pipelines` and `pipeline_versions` (§2); (b) store the script on an agent version as a "no-model agent" | **(a)**. It's a different runtime (no model, no thread) with different limits. It shares the versioning helpers, not the tables. |
| A6 | **Prompt injection at build time.** The scout reads untrusted pages, and later steps hold tools that change the schema and schedule code. | Each such tool pauses for a member, who sees the exact SQL, script, domains and schedule. Is that enough? | The fixed steps already split the reader (scout) from the writers (target, author, publish), and each writer's tool pauses for a member. That's enough for v1, as long as each card shows the **exact** artefact (the full script, diffed against the previous version) and not the model's summary of it. |
| A7 | **Approvals in a headless context.** | A build is always started by a member, but `request_approval` in an unattended workflow waits up to 7 days. | Keep the expiry. Expired counts as rejected, and the agent is told so. |

## Phases

Each phase is a mergeable PR (or a small stack), and each is useful on its own.

1. **Generic approval requests** (D2, D3). Request-kind registry, `approval_requests`, waiting by
   kind and id (migration of `waiting_on_request_id`), the `request_approval` tool, the card
   with Approve, Reject and Reply in chats and run pages, notifications, expiry, and the
   dependents and cancel paths. Tests mirror `test_schema_change_requests` and
   `test_workflow_pause`.
2. **Pipelines and the runtime**:
   - `pipelines`, `pipeline_versions`, `pipeline_runs` and `pipeline_state`;
   - runner `/files` endpoints;
   - sandbox sessions not tied to agent runs;
   - the `pipelines` queue and worker;
   - the loader for records (on `write_data`'s planner);
   - a Pipelines page with versions, runs and "Run now";
   - pipelines can be created by hand with a pasted script, which tests the runtime without
     the builder.
3. **Document folders** (A3): store migration `0008` (`document_folders`, `documents.folder_id`,
   `external_id`, `source_url`), the loader for documents, and folders on the Data page.
4. **Scheduling pipelines**: `TriggerTargetKind.pipeline`, the scheduler path, the floor and
   the daily cap, failure notifications, and pausing after repeated failures.
5. **Builder tools**: `run_python` with the output contract and domain grants for a workflow run (D8),
   `create_document_folder`, `test_pipeline`, `save_pipeline`, and the pipeline draft (D11).
6. **The builder workflow**: built-in workflows (`workflows.builtin`), the four step agents
   and their skills, the draft shown on the run page, the New pipeline form, and an
   end-to-end test with the fake model (`tests/fake_model.py`) driving a build through all four
   steps and three approvals against a local HTTP fixture.
7. **Later**: organisation secrets (D5), repair builds and then auto-repair (D7), rewinding a
   workflow run to a step (D9), code nodes (D10), mixed targets (A2), and pipeline lineage on
   rows and documents (which run wrote what).

## Risks

- **Output size**: a source that returns gigabytes doesn't fit the output contract in one run.
  The builder must write incremental scripts (cursor in state), and the first run's backfill
  may take many runs. `test_pipeline` should flag a projected run size over the cap.
- **Sandbox start latency** on Fargate (tens of seconds) is fine for a schedule, but it sets a
  practical floor on frequency. Most sources don't need more than hourly.
- **Terms of use and robots**: the source-approval card should carry the builder's notes on
  licence and terms, so the member makes that call knowingly. The platform doesn't enforce it.
- **Generated code quality**: scripts are reviewed by members who may not read Python. The
  final card should lead with a plain summary (what it fetches, from where, how often, how
  much), and show the code below it.
