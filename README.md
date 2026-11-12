# aios

A platform where teams work with managed agents and workflows that hold the context the team
needs in a store the organisation owns.

- **Documents.** Upload files to the organisation's store; they're parsed and chunked with
  Docling, embedded with Voyage, and searched by a hybrid search (vector plus full-text, fused
  with Reciprocal Rank Fusion).
- **Agents and skills.** Define agents (a base prompt, reusable skills and tools from a built-in
  library: web search, document and run history search, Run Python in a sandbox, and tools to
  read, write and ask for data tables) and chat with them. Agents, skills and workflows are
  versioned, and every run is pinned to a version.
- **Workflows.** Graph agents and other workflows together, and run them on a worker.
- **Chats.** Group conversations of people, agents and workflows. Mentioning an agent or a
  workflow briefs it on the chat and starts a run, whose card and result appear in the chat.
- **Triggers.** Schedules start agents and workflows without anyone in the loop.
- **Stores.** Each organisation has its own Postgres database: its documents, the data tables
  its agents asked for (each new one approved by a member), and, if switched on, a copy of its
  run history.
- **Organisations.** Everything belongs to the organisation: every member sees and works with
  what any member made. People join with invitation links.

The words used here and in the docs are the glossary's: [`CONTEXT.md`](CONTEXT.md).

## Architecture

```
browser ──▶ ALB / nginx ──┬─ /api/* ──────────▶ api (FastAPI + Uvicorn)
                          └─ everything else ─▶ web (React SPA, static: /login and /register too)
api ──enqueue──▶ Redis ───▶ worker (Celery: Docling ▶ HybridChunker ▶ Voyage embeddings)
api ──enqueue──▶ Postgres work queue ──▶ run-service (agent turns, workflow runs node by node; api image)
api, worker ──▶ Postgres 17 + pgvector: the platform database, as `aios_app`, and each organisation's store (runs and their history: LangGraph checkpoints, each store's `langgraph` schema)
api, worker ──▶ S3 (RustFS locally): uploaded files, under each store's prefix
browser ──download link (presigned, a minute)──▶ S3
api, worker ──traces──▶ Langfuse (optional, self-hosted)
```

The processes:

- **api**: the HTTP API, and the agent chat turns of a run's page (streamed).
- **run-service**: mentioned agents' turns, the handoff before every mention, workflow runs
  node by node, resuming runs once a schema change is decided, and the schedules, claimed
  fairly from a work queue (see [Run service](docs/run-service.md),
  [Workflows](docs/workflows.md)).
- **web**: the React SPA, served as static files.
- **ingest-worker**: Celery's `ingest` queue: document ingest (see [Stores](docs/stores.md)) and
  embedding the search index.
- **urgent-worker**: Celery's `urgent` queue: applying approved schema changes, and restores, which
  never wait behind routine maintenance.
- **routine-worker**: Celery's `routine` queue: creating, migrating, measuring and deleting each
  organisation's store, purging, expiry, retention and sweeps; it runs the beat, which only
  enqueues (the queue metrics among it). Only the two maintenance workers hold the
  provisioner's credentials.
- **sandbox**: where Run Python's code runs; on AWS, one Fargate task per agent run (see
  [Deploy](docs/deploy.md#sandboxes-on-fargate-run_python)).

Each organisation's store is a database of its own on the same Postgres instance as the
platform database, which knows who people are, which organisations exist, and how to reach each
store.

| Layer | Stack |
| --- | --- |
| Backend | Python 3.13, uv (exact pins, `exclude-newer = "7 days"`), hatchling, FastAPI + Uvicorn, Celery, pydantic-settings, SQLAlchemy + Alembic, psycopg 3 |
| Auth | Our own accounts: email and password (Argon2id via `pwdlib`, emails checked and normalised by `email-validator`); Starlette `SessionMiddleware` (itsdangerous-signed cookie) holds the user id and their session version. See [Accounts](docs/accounts-and-organisations.md#accounts) |
| Frontend | React + TypeScript, Vite, pnpm (`minimumReleaseAge: 10080` = 7 days), Tailwind v4 + shadcn/ui, Vercel AI SDK `useChat`, react-markdown + remark-gfm; markdown editing via `@github/markdown-toolbar-element` on plain textareas |
| Retrieval | pgvector HNSW (cosine) + Postgres FTS (`tsvector`, GIN), RRF (k=60) |
| Inference | LangGraph agents (LangChain `create_agent`, `anthropic:claude-sonnet-5-5` via `langchain-anthropic`), streamed to the UI by our own Vercel AI SDK adapter (`ui_stream.py`); LangGraph Postgres checkpointer for run history; `langchain-voyageai` embeddings (`voyageai:voyage-4`, 1024-d); Docling `HybridChunker` |
| Tracing | Langfuse (open source, self-hosted) via its LangChain callback handler |
| Infra | Docker Compose locally (RustFS for S3); Terraform on AWS: VPC, ECS Fargate, ALB, RDS Postgres, ElastiCache (Valkey), S3 (SSE-KMS), ECR, Secrets Manager |

## Quick start

You need an Anthropic API key and a Voyage AI API key. The full steps, the code layout, the
checks and debugging with Claude Code are in [Development](docs/development.md).

### In Docker

```sh
cp .env.example .env     # fill in the API keys and a SESSION_SECRET
docker compose up --build
open http://localhost:8080    # create an account at /register
```

Compose creates the database roles, migrates the platform database and the stores, and stands
RustFS in for S3.

### Locally

```sh
docker compose up -d db redis rustfs

cd backend
cp .env.example .env     # PUBLIC_URL=http://localhost:5173
uv sync --extra ingest   # drop --extra ingest if you're not running the worker
uv run python -m aios.db_roles   # as the master user (DATABASE_ADMIN_URL); safe to re-run
uv run python -m aios.datastores.files create-bucket   # uploaded files' bucket, in RustFS
uv run alembic upgrade head
uv run python -m aios.datastores.provision migrate-all   # stores, after every upgrade
uv run uvicorn aios.main:app --reload
uv run celery -A aios.worker worker -Q ingest --loglevel=INFO   # separate shell
uv run python -m aios.run_service   # another: agent turns, workflow runs, schedules
uv run celery -A aios.worker worker -Q urgent,routine -B --loglevel=INFO   # maintenance (-B: the beat)

cd frontend
pnpm install
pnpm dev                 # http://localhost:5173, proxies /api to :8000
```

Checks: `uv run ruff check . && uv run ruff format --check . && uv run pytest` in `backend`,
`terraform test` in `infra/terraform`, and `pnpm build && pnpm test && pnpm test:e2e` in
`frontend`. What each needs is in [Development](docs/development.md#checks-and-tests).

To deploy to AWS, see [Deploy](docs/deploy.md).

## Repository

```
backend/            FastAPI app, Celery workers, Alembic migrations (platform and store trees), tests
frontend/           Vite React SPA, its browser tests, and the nginx images
infra/terraform/    AWS, with tests against a mocked AWS
infra/deploy.sh     build, push, create roles, migrate, migrate stores, roll out
infra/claude/       Claude Code image for debugging the Compose stack
docs/               one page per domain, decision records, roadmap, specs and tickets
plan/               design plans behind the specs
CONTEXT.md          the glossary
```

The full layout of `backend/src/aios` is in [Development](docs/development.md#layout).

## Docs

- [The docs index](docs/README.md): one page per domain, from
  [accounts and organisations](docs/accounts-and-organisations.md) and [stores](docs/stores.md)
  to [agents and skills](docs/agents-and-skills.md), [tools](docs/tools.md),
  [workflows](docs/workflows.md), [chats](docs/chats.md), [triggers](docs/triggers.md),
  [development](docs/development.md), [deploy](docs/deploy.md) and [tracing](docs/tracing.md).
- [The glossary](CONTEXT.md): the words the product and its code use.
- [Decision records](docs/decisions/): why aios is shaped as it is, starting with
  [0001](docs/decisions/0001-organisation-database-as-system-of-record.md).
- [The roadmap](docs/roadmap.md): what aios will need later, and why each item waits.
- [Specs](docs/specs/) and [tickets](docs/tickets/README.md): the work under way.
