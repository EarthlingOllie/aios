# Ingestion pipelines: an agent that compiles a data request into a scheduled Python script

Status: **draft plan** (2026-10-20). Nothing here is built, and the questions in
[Architectural ambiguities](#architectural-ambiguities) need answers first. It picks up the
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
  with one live agent of each kind per organisation.
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
  upserts on the table's primary key or a declared unique key. That gives the same catalog
  checks, type coercion, embeddings, quota and row caps. Each run writes in batches, and a
  failed run writes nothing (one transaction per run, or per batch with run-level staging:
  see A8).
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
  2. start a **fresh sandbox per run**, with the version's `allowed_domains` and secrets (A6);
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

### 6. The builder

The builder is a **built-in agent** (`agents.builtin = 'pipeline_builder'`, made on demand like
the documents assistant). Its skill is the build procedure, and its tools are:

| Tool | Status | Use |
| --- | --- | --- |
| `web_search` | exists | Find candidate sources |
| `run_python` | exists, extended | Probe sources, write and test the script (output contract, domain grants: A4) |
| `describe_data`, `query_data` | exist | See the store's tables, and check what a test produced |
| `request_approval` | new (§1) | Choose a source, then review the plan; returns feedback |
| `request_schema_change` | exists | Create or extend the target tables |
| `create_document_folder` | new | Make the folder for an unstructured target (no approval: it's empty and harmless) |
| `test_pipeline` | new | Run the draft script in the run's sandbox with an empty or given state, and validate `/out` against the target without writing. Returns counts, a sample and errors. |
| `save_pipeline` | new | Propose the final pipeline (script, domains, target, cron), which pauses for approval. Once approved, it creates the pipeline version and its schedule, and optionally a first run. |

The procedure the skill describes:

1. Restate the request.
2. Search and probe.
3. Call `request_approval` with 1–3 source options, each with its domains, auth needs, cost,
   freshness and licence or terms notes. Loop on feedback.
4. Classify the data as structured or unstructured. Then either call `describe_data` and
   `request_schema_change`, or call `create_document_folder`.
5. Write the script, then run `test_pipeline` until it's clean.
6. Choose a frequency.
7. Call `save_pipeline`, which is the final approval. Loop on feedback.

A member starts a build from a **New pipeline** form on the Pipelines page, which starts a run
of the builder with the request as its first message. The builder can also be mentioned in a
chat like any agent. Either way, the conversation is the run page, with the approval cards in
it.

## Architectural ambiguities

These need decisions. Each has a recommendation.

| # | Question | Options | Recommendation |
| --- | --- | --- | --- |
| A1 | **What is "the workflow"?** In aios a workflow is a DAG of agent nodes that pass text along edges, and it has no loops. The build process loops (feedback, then a re-proposal; a test, then a fix). | (a) one built-in builder agent with a skill and tools; (b) a built-in workflow of several agents (scout → designer → coder), with approvals as pauses between them; (c) a new orchestration primitive | **(a)**. Loops are natural in one agent's turn, and the pause machinery already works in chats and workflows, so it can be wrapped in a workflow later. (b) can't loop back on feedback without cycles. |
| A2 | **Generalise the request machinery, or add a parallel one?** | (a) a request-kind registry, with waiting by kind and id; (b) copy the schema change path for approvals | **(a)**. It touches `workflows.py`, `api/agents.py`, `schema_changes.cancel` and the dependents check, but every later pause kind gets it for free. |
| A3 | **Where does the data get written from?** The sandbox has no route to the database or S3. | (a) the output contract plus a trusted loader (§3); (b) give the sandbox scoped credentials (a per-pipeline role with `INSERT` on its tables, presigned PUT URLs), which needs a network path from sandbox subnets to RDS and S3 | **(a)**. It keeps the sandbox invariant, and reuses `write_data`'s checks, quota and embeddings. Costs: the output is bounded by the cap, and a very large backfill has to be paginated across runs through the state cursor. |
| A4 | **Build-time egress.** The builder probes domains it only finds while running, but `run_python`'s allowlist is fixed in the agent version's settings. | (a) the approved source grants its domains to the run (`run_domain_grants`); the effective allowlist is the settings plus the grants, and the session's proxy token is re-minted (sandbox restarted, or a token per exec); (b) give the builder a wildcard allowlist; (c) only `web_search` at build time, and the member lists domains up front | **(a)**. Approving a source approves reaching it. Before that, the builder only sees `web_search` results. (b) makes the builder an open egress for prompt-injected content. |
| A5 | **Structured or unstructured: who decides, and what about mixed records** (tickets, filings with metadata)? | (a) the builder decides one target, and the member confirms in the approval; (b) allow both, with rows in a table plus documents linked by `external_id` | **(a) for v1**, with the contract already allowing both directories, so (b) is only a loader change later. |
| A6 | **Credentials for sources** (API keys, tokens). There is no secrets model today. | (a) v1 supports keyless sources only; (b) organisation secrets: encrypted like data store passwords, entered on a page (never in chat, never seen by a model), named in the version, and injected as env vars into that pipeline's sandbox only; (c) OAuth connections | **(a) now, (b) next.** (b) also needs the builder's test runs to receive the secret, and a sandbox with a secret can still send it to its allowed domains, which is acceptable since those are the source's. |
| A7 | **What is a "directory" in S3?** Documents are flat today. | (a) a store table `aios.document_folders` (name, description, `pipeline_id`), with `documents.folder_id` and `documents.external_id`, keys under `folders/<id>/`, and the Data page's Documents tab grouped by folder; (b) a bare S3 prefix with no rows | **(a)**. It's a store migration (`0008`). Ingested files then show on the Data page, count against the files quota, and are found by document search (which could filter by folder later). |
| A8 | **Atomicity and idempotency of a load.** | (a) one transaction per run (bounded by the output cap); (b) a staging table, then swap; (c) batches, made idempotent by requiring an upsert key | **(c) with (a) where it fits.** Structured targets must declare an upsert key, which the builder sets in the schema change. Document dedup is by `external_id` or hash. A re-run is then always safe. |
| A9 | **Approval gates: how many, and who approves?** | Source choice; schema change (exists); final pipeline. Approver: any member (as for schema changes), or the requester only | **Three gates, any member**, matching schema changes. Every new pipeline version, including an edit, goes through the final gate again. Should a pipeline's **first run** need its own approval after seeing real output? We suggest not: `test_pipeline`'s sample is on the final card. |
| A10 | **When the source drifts or the script breaks.** | (a) failures notify, and a member starts a repair build (the builder, given the version and the failing run); (b) auto-repair: a failure starts the builder, which proposes a new version through approval | **(a) for v1.** (b) is the natural next step, and since the result still goes through approval, it's safe. |
| A11 | **Who owns runs and costs?** A scheduled run has no member. | As schedules do: `created_by` null and `trigger_id` set. Quotas: the data store caps already bound storage; is a per-organisation cap on sandbox minutes needed? | Follow schedules. Add `PIPELINE_MAX_RUNS_PER_DAY` per organisation as a cheap guard until billing exists. |
| A12 | **Is a pipeline its own entity, or a target kind of agent?** | (a) new `pipelines` and `pipeline_versions` (§2); (b) store the script on an agent version as a "no-model agent" | **(a)**. It's a different runtime (no model, no thread) with different limits. It shares the versioning helpers, not the tables. |
| A13 | **Prompt injection at build time.** The builder reads untrusted pages and holds tools that change the schema and schedule code. | Each of those tools pauses for a member, who sees the exact SQL, script, domains and schedule. Is that enough, or should the builder's tool set be split (a reader agent without write tools)? | The gates are enough for v1, as long as each card shows the **exact** artefact (full script, diffed against the previous version) and not the model's summary of it. |
| A14 | **Approvals in a scheduled or headless context.** | A pipeline build is always member-initiated, but `request_approval` in an unattended workflow waits up to 7 days. | Keep the expiry. Expired counts as rejected, and the builder is told so. |

## Phases

Each phase is a mergeable PR (or a small stack), and each is useful on its own.

1. **Generic approval requests** (A2). Request-kind registry, `approval_requests`, waiting by
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
3. **Document folders** (A7): store migration `0008` (`document_folders`, `documents.folder_id`,
   `external_id`, `source_url`), the loader for documents, and folders on the Data page.
4. **Scheduling pipelines**: `TriggerTargetKind.pipeline`, the scheduler path, the floor and
   the daily cap, failure notifications, and pausing after repeated failures.
5. **Builder tools**: `run_python` with the output contract and domain grants (A4),
   `create_document_folder`, `test_pipeline`, and `save_pipeline`.
6. **The builder**: built-in `pipeline_builder` agent and skill, the New pipeline form, and an
   end-to-end test with the fake model (`tests/fake_model.py`) driving a build against a local
   HTTP fixture.
7. **Later**: organisation secrets (A6), repair builds and auto-repair (A10), mixed targets
   (A5), and pipeline lineage on rows and documents (which run wrote what).

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
