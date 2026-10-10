# Data ingestion pipelines: working plan

Status: **draft from the first discussion** (2026-10-10). Later discussions on individual
aspects will be consolidated into a fuller version.

Codebase reference point: PR #3 (tool library and web search), stacked on PR #2 (agents and
skills).

## Goal

A user describes their data sources and how to fetch them, and an agent builds the pipeline.

- **Structured data** goes into Postgres, in a database the agent controls and that is isolated
  from the core service's database.
- **Unstructured data** goes into the vector store.
- Both feed a **RAG agent** that answers questions over the ingested data.

## How to read the flags

Each item has a status and an origin.

| Flag | Meaning |
| --- | --- |
| `[EXISTS]` | Already in the codebase |
| `[DECIDED]` | Agreed in discussion |
| `[PROPOSED]` | Suggested, not agreed yet |
| `[OPEN]` | A question that needs a decision |
| `[RISK]` | A weakness or danger to design around |
| `[GAP]` | Missing work the feature depends on |

Origin: **(user)** means it came from you, and **(review)** means it came from the design
review.

---

## 1. What the codebase already gives us

- `[EXISTS]` **Unstructured ingestion pipeline.** `ingest.py` runs Docling conversion, the
  HybridChunker and Voyage embeddings (`voyage-4`, 1024-d), and writes rows to `chunks`.
- `[EXISTS]` **Hybrid retrieval.** `search.py` combines pgvector HNSW cosine search with Postgres
  full-text search, fused with RRF (k=60). Results are scoped by `documents.owner_id`.
- `[EXISTS]` **Reliable background worker.** The Celery worker claims a row atomically, counts
  attempts with `MAX_ATTEMPTS`, uses `acks_late` with `reject_on_worker_lost`, and sets soft and
  hard time limits.
- `[EXISTS]` **Tool registry.** `aios/tools/` defines a `ToolSpec` with a key, a Pydantic
  config model and a `build()` method. Agent versions pin tools and their settings.
- `[EXISTS]` **Immutable versioning pattern.** Agents and skills are made of an identity row
  plus immutable versions, compared by config hash. Runs are pinned to a version.
- `[EXISTS]` **A RAG agent, but only the built-in one.** `agent.py` calls `search_documents`.
  User-defined agents (`runner.py`) only get registry tools, which today means just web search.
- `[EXISTS]` **Vector store is the core database.** pgvector runs in the same Postgres instance
  as the core tables, not in a separate vector database.

## 2. Pipeline building: who builds what, and when

- `[DECIDED]` (user) An agent builds the pipeline from the user's description.
- `[PROPOSED]` (review) **The agent as compiler, not runtime.** The agent produces a pipeline
  artifact that a human reviews. An ordinary worker then runs it deterministically on a
  schedule. The agent is called again only when a run fails or the source's shape drifts.
  - The alternative, where an LLM fetches and transforms data on every run, is expensive,
    non-deterministic, hard to debug and drifts silently. `[RISK]`
- `[PROPOSED]` (review) **Pipeline specs are versioned** like agent versions: an identity row
  plus immutable spec versions, with each run pinned to a version.
- `[PROPOSED]` (review) **Declarative specs first, generated code later.** Start with a fixed
  library of connector types, such as HTTP/REST, RSS, S3, database and file drop, each a
  `ToolSpec`-like registry entry. The agent picks a connector, fills in its config (endpoint,
  pagination, field mapping), proposes a target schema and runs a dry run on a sample.
  Free-form generated code comes later, if at all.
- `[PROPOSED]` (review) **Human approval** of the spec, including any schema it proposes,
  before the first real run.
- `[PROPOSED]` (review) **Eval questions as part of the build.** For each source, the build
  agent also writes a few questions with expected answers, so the RAG agent's results measure
  whether the pipeline works.
- `[OPEN]` Does the build happen in a chat with the agent, as an agent run, or through a
  dedicated pipeline-builder UI?

## 3. Structured data store (agent-controlled Postgres)

- `[DECIDED]` (user) Structured data goes into Postgres.
- `[DECIDED]` (user, endorsed by the review) **Isolated from the core service database**,
  because the agent needs to change schemas. It must never share a database or role with
  `users`, `agent_versions`, sessions and so on. Isolation also allows separate backups, quotas
  and a separate `statement_timeout`.
- `[RISK]` (review) **Isolation protects the core service, not the user's data.** An agent that
  drops a column or changes a type destroys history.
- `[PROPOSED]` (review) Ways to limit schema changes:
  - one Postgres schema per dataset or per user
  - additive-only migrations by default (new tables and columns)
  - destructive changes require approval
  - every DDL change is stored as a versioned migration record, never as an ad-hoc `ALTER`
- `[PROPOSED]` (review) **Land raw data first.** Raw records go into an append-only JSONB table,
  then the agent builds typed tables from it. A bad schema then means a rebuild, not data loss.
- `[PROPOSED]` (review) **The build agent writes a data catalog** describing each table and
  column. The RAG agent's text-to-SQL depends on it (see section 6).
- `[OPEN]` A separate RDS instance, a separate database on the same instance, or just a
  separate schema and role? This trades cost against how complete the isolation is.

## 4. Unstructured data store (vector)

- `[DECIDED]` (user) Unstructured data goes into the vector store.
- `[OPEN]` (review) **Which vector store?** The plan said "vector db", but today pgvector runs in
  the core Postgres. Agent-ingested chunks could go into:
  - the core `chunks` table. The core service owns its schema and the agent never changes it.
    `[PROPOSED]`
  - the isolated agent database
- `[PROPOSED]` (review) **The vector schema is fixed and owned by the core service.** The agent
  only supplies documents. Agent-defined schemas exist only on the structured side.
- `[GAP]` (review) **Sync support on `documents` and `chunks`.** Synced sources need:
  - a `sources` table, and `source_id` on documents
  - an external ID and content hash for deduplication and incremental updates
  - deletion handling, so chunks go when their source item goes
  - source metadata in `chunks.meta`: timestamps, and a link back to the original item for
    citations
- `[RISK]` (review) **Raw bytes are stored in Postgres** (`documents.data`, `LargeBinary`).
  This is fine for uploads but won't scale to synced sources. Consider object storage such as
  S3.
- `[PROPOSED]` (review) **Mixed records** such as tickets, emails and CRM rows have structured
  metadata plus long text. Store them on both sides, rows in Postgres and embedded text in
  `chunks`, linked by ID. Plan for this from the start rather than forcing each source into one
  store.

## 5. Execution, security and secrets

- `[GAP]` `[RISK]` (review) **No sandbox for agent-written fetch or transform code.** The Celery
  worker today runs with full `DATABASE_URL` access and the app's secrets. Pipeline runs need:
  - a separate execution environment, such as a dedicated ECS task, Firecracker or gVisor
  - no credentials for the core database
  - egress controls
  - CPU, memory and time limits

  Declarative specs (section 2) avoid much of this in v1.
- `[RISK]` (review) **Prompt injection from fetched content.** Scraped pages and API payloads are
  untrusted input to an LLM. Agents that read that data must not hold DDL rights at the same
  time.
- `[GAP]` (review) **No per-user secrets model.** Sources need API keys, OAuth tokens and
  database passwords. That requires:
  - encrypted credential storage per source
  - injection into the sandbox only
  - the agent refers to secrets by name and never sees their values
- `[OPEN]` (review) **Multi-tenancy in the agent database.** Proposed: one Postgres schema per
  user with a dedicated role and limited `search_path` and grants. Row-level security is hard to
  enforce on tables the agent creates itself.

## 6. Consumption: the RAG agent

- `[DECIDED]` (user) Ingested data feeds a RAG agent.
- `[GAP]` (review) **Step 1: move `search_documents` into the tool registry** so any
  user-defined, versioned agent can do retrieval, not only the built-in chat agent.
- `[PROPOSED]` (review) **Search filters**, such as source and date range, in the search tool.
  They become extra `WHERE` clauses in the hybrid SQL.
- `[PROPOSED]` (review) **Per-agent source scoping.** An agent version pins which sources it can
  search, the same way it pins skills and tools.
- `[RISK]` (review) **Vector search doesn't work for structured questions**, such as "total
  refunds last quarter". The options are:
  - **text-to-SQL:** a read-only `query_data` tool. It's powerful but depends on the data
    catalog. Without one, SQL over agent-invented schemas will be unreliable.
  - **rows as text:** render each row or entity as a passage in `chunks`. This is simple but
    handles lookups only, not aggregates.
  - **both:** the agent picks a tool per question. `[PROPOSED]`, with `query_data` shipped
    after the catalog exists.
- `[PROPOSED]` (review) **Guardrails for `query_data`:**
  - a read-only role scoped to the user's schema
  - a statement timeout and a row limit
  - every query recorded in the run trace
- `[RISK]` (review) **Ingested content goes into the RAG agent's context.** If that agent also
  has web search or action tools, a poisoned document can steer it. Keep the tool set of
  retrieval agents minimal, and record the source of each retrieved chunk in the run trace.
- `[PROPOSED]` (review) **Citations and provenance.** `SearchHit` already carries the filename
  and page metadata. Add the external URL and the source now; it's cheap now and painful to
  backfill later.
- `[RISK]` (review) **Freshness.** A RAG agent quoting stale or deleted content is a visible
  failure, so incremental sync and deletion are core requirements, not extras.

## 7. Operations

- `[GAP]` (review) **Scheduling:** Celery beat or EventBridge.
- `[GAP]` (review) **Incremental sync:** cursors or watermarks per source, backfills and
  rate-limit handling.
- `[GAP]` (review) **Run history** per pipeline run: rows and documents processed, errors and
  duration.
- `[GAP]` (review) **Drift detection and alerts** when a source changes shape, which is the
  trigger for calling the build agent again (section 2).
- `[RISK]` (review) **Most of the long-term cost is in running pipelines, not building them**,
  and that is ordinary infrastructure work.

## 8. Strengths of the idea (summary)

- (review) It builds on existing, proven pieces: Docling, chunking, Voyage, pgvector hybrid
  search, the Celery worker and the tool registry.
- (review) Isolating the agent database is the right security boundary.
- (review) Users describe what they want, and the agent does the dull work of field mapping,
  pagination and reading API docs.
- (review) The structured/unstructured split matches how each kind of data is queried.
- (review) The RAG agent gives the pipeline a measurable goal, which makes the build agent
  possible to evaluate.
- (review) The immutable versioning pattern carries straight over to pipeline specs.

## 9. Proposed order (draft)

1. Move `search_documents` into the tool registry, with source filters.
2. Add a `sources` table, `source_id` on documents, and deduplication and deletion by external
   ID.
3. Add declarative connectors and a build agent that produces versioned specs for unstructured
   sources. Their output goes through the existing `chunks` pipeline.
4. Add the isolated database for structured data, with a schema and role per user and a
   catalog written by the build agent.
5. Add a `query_data` read-only SQL tool for the RAG agent.

Steps 1–3 deliver RAG over user-defined sources without the riskiest pieces: generated DDL,
the second database and the SQL tool.

## 10. Topics for follow-up discussions

- [ ] Compiler vs runtime, and the format of the pipeline spec (section 2)
- [ ] Isolation level and tenancy model for the agent database (sections 3 and 5)
- [ ] Choice of vector store, and the schema changes to `chunks` and `documents` (section 4)
- [ ] Sandbox and secrets design (section 5)
- [ ] RAG retrieval over structured data: SQL, rows as text, or both (section 6)
- [ ] Scheduling, sync and observability (section 7)
- [ ] The UX for describing a source and approving a pipeline
- [ ] Evaluation: how we know a pipeline and the RAG agent are working

## Discussion log

| Date | Topic | Outcome |
| --- | --- | --- |
| 2026-10-10 | Initial strengths and weaknesses review | This draft |
