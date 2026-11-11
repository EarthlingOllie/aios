# Development

How aios fits together, how to run it on your machine, where the code lives, and the rules for
changing it.

## Architecture

The same as in the [README](../README.md), kept here so the docs stand alone.

```
browser ──▶ ALB / nginx ──┬─ /api/* ──────────▶ api (FastAPI + Uvicorn)
                          └─ everything else ─▶ web (React SPA, static: /login and /register too)
api ──enqueue──▶ Redis ──┬─▶ worker (Celery: Docling ▶ HybridChunker ▶ Voyage embeddings)
                         └─▶ workflow-worker (Celery, `workflows` queue: workflow runs; api image)
api, worker ──▶ Postgres 17 + pgvector, as `aios_app` (agent run history: LangGraph checkpoints, `langgraph` schema)
api, worker ──▶ S3 (RustFS locally): uploaded files, under each store's prefix
browser ──download link (presigned, a minute)──▶ S3
api, worker ──traces──▶ Langfuse (optional, self-hosted)
```

The processes:

- **api**: the HTTP API, agent chat turns (streamed), and, in each process, the scheduler and
  the agent turns that schedules and chat mentions start (see [Triggers](triggers.md)).
- **web**: the React SPA, served as static files.
- **worker**: document ingest (see [Stores](stores.md)) and the run history export.
- **workflow-worker**: workflow runs, and resuming runs once a schema change is decided (see
  [Workflows](workflows.md)).
- **provisioning-worker**: creating, migrating, measuring and deleting each organisation's
  store, and applying schema changes (see [Stores](stores.md)).
- **sandbox**: where Run Python's code runs; on AWS, one Fargate task per agent run (see
  [Deploy](deploy.md#sandboxes-on-fargate-run_python)).

Each organisation's store is a database of its own on the same Postgres instance as the
platform database, which knows who people are, which organisations exist, and how to reach each
store.

| Layer | Stack |
| --- | --- |
| Backend | Python 3.13, uv (exact pins, `exclude-newer = "7 days"`), hatchling, FastAPI + Uvicorn, Celery, pydantic-settings, SQLAlchemy + Alembic, psycopg 3 |
| Auth | Our own accounts: email and password (Argon2id via `pwdlib`, emails checked and normalised by `email-validator`); Starlette `SessionMiddleware` (itsdangerous-signed cookie) holds the user id and their session version. See [Accounts](accounts-and-organisations.md#accounts) |
| Frontend | React + TypeScript, Vite, pnpm (`minimumReleaseAge: 10080` = 7 days), Tailwind v4 + shadcn/ui, Vercel AI SDK `useChat`, react-markdown + remark-gfm; markdown editing via `@github/markdown-toolbar-element` on plain textareas |
| Retrieval | pgvector HNSW (cosine) + Postgres FTS (`tsvector`, GIN), RRF (k=60) |
| Inference | LangGraph agents (LangChain `create_agent`, `anthropic:claude-sonnet-5-5` via `langchain-anthropic`), streamed to the UI by our own Vercel AI SDK adapter (`ui_stream.py`); LangGraph Postgres checkpointer for run history; `langchain-voyageai` embeddings (`voyageai:voyage-4`, 1024-d); Docling `HybridChunker` |
| Tracing | Langfuse (open source, self-hosted) via its LangChain callback handler |
| Infra | Docker Compose locally (RustFS for S3); Terraform on AWS: VPC, ECS Fargate, ALB, RDS Postgres, ElastiCache (Valkey), S3 (SSE-KMS), ECR, Secrets Manager |

## Prerequisites

An **Anthropic** API key and a **Voyage AI** API key. Nothing else: aios has its own accounts
(see [Accounts and organisations](accounts-and-organisations.md#accounts)).

## Layout

```
backend/            FastAPI app, Celery worker, Alembic migrations, tests
  src/aios/
    main.py         app + SessionMiddleware
    auth.py         /api/auth/{options,register,login,logout}, start_session, current_user,
                    current_organisation
    passwords.py    Argon2id hashing and checking
    api/me.py       /api/me: GET (with the user's organisations), PATCH (name), PUT email and
                    password
    organisations.py  organisations, which own what their members make: creating one, a
                    person's memberships (any number), invitation links, joining and
                    leaving (handing over the chats a leaver owned)
    api/documents.py upload/list/download/delete, GET /api/o/{org}/search
    api/skills.py   CRUD /api/o/{org}/skills
    api/agents/     CRUD /api/o/{org}/agents, versions, runs: /api/o/{org}/agents/{id}/runs[/{run_id}[/chat]]
                    (crud, versions, runs, chat: a run's chat stream)
    api/workflows.py CRUD /api/o/{org}/workflows, versions and restore, runs: /api/o/{org}/workflows/{id}/runs
    api/triggers.py CRUD /api/o/{org}/triggers (schedules), schedule preview, timezones
    api/chats/      CRUD /api/o/{org}/chats, members: /api/o/{org}/chats/{id}/members[/{member_id}], mentionable, messages
                    (crud, members, messages, invocations)
    api/organisation.py POST /api/organisations, GET/PUT /api/o/{org}/organisation (the Organisation page), invitation
                    links (/api/o/{org}/organisation/invites, /api/invites/{token}[/accept]), leaving
    api/provenance.py who made and last changed an organisation's things, as the API reports it
    api/settings.py GET/PUT /api/o/{org}/organisation/settings (the organisation's: the run history export)
    scheduler.py    fires due schedules (runs in each API process)
    firing.py       runs an agent's turn for a cause outside a request: the cap, heartbeat, stall sweep
    invocations.py  what a mention in a chat starts: invocations, their cards and results posted back
    cron.py         cron expressions in a timezone (cronsim)
    workflows/      workflow graph validation, and running workflows (validation, execution, steps)
    dependents.py   what depends on an agent (runs, workflows, schedules), checked before deleting it,
                    and what uses it, shown before archiving it
    seed.py         `make seed`: rebuilds development data from scratch
    runner.py       builds the LangGraph agent for a turn of a user-defined agent's run
    checkpoints.py  run history: LangGraph's Postgres checkpointer, one thread per run
    ui_stream.py    Vercel AI SDK UI message stream <-> LangChain messages / LangGraph streams
    llm.py          the chat model (LangChain "provider:model" names)
    tracing.py      Langfuse callbacks and trace metadata
    versioning.py   creating agent/skill/workflow versions, rendering instructions
    tools/          the tool library agents can be given (registry + one module per tool)
    api/tools.py    GET /api/tools (the library, with each tool's settings schema)
    sandbox/        sandboxes for the run_python tool: sessions, backends, the in-sandbox
                    runner and the egress proxy
    agent.py        the run context (`ChatDeps`) agents' tools read
    assistant.py    the documents assistant: the built-in agent each new chat gets
    datastores/     stores: data_store_for(organisation), the one place an organisation
                    becomes a store, store_session(id, role), the only way to open a session on
                    an organisation's data,
                    models.py (the tables every store has: documents, chunks; and the
                    run history export's), history.py (the run history export),
                    provision.py (creating, migrating and deleting each store's database),
                    files.py (uploaded files in S3, and download links), schema_changes/
                    (schema change requests: spec, compile, apply, expiry), and
                    migrations/ (the store Alembic tree)
    search.py       hybrid search SQL (RRF)
    ingest.py       Docling convert → HybridChunker → embed → store
    worker.py       Celery app + ingest, workflow run and store provisioning tasks
    db_roles.py     creates the database roles (`aios_app`, `aios_provisioner`) as the master user
  sandbox/          the sandbox image's pinned libraries (requirements.in/.txt)
  Dockerfile        targets: api, worker, sandbox
frontend/           Vite React SPA (+ nginx images/configs)
infra/terraform/    AWS
infra/deploy.sh     build → push → create roles → migrate → migrate stores → roll out
infra/claude/       Claude Code image for debugging the Compose stack
docker-compose.yml  db, redis, rustfs, files-bucket, db-roles, migrate, migrate-stores, api,
                    worker, workflow-worker, provisioning-worker, web, sandbox
                    (+ claude, docker-proxy with --profile debug)
docs/               these pages, decision records, roadmap, specs and tickets
plan/               design plans behind the specs
CONTEXT.md          the glossary
```

## Run everything in Docker

```sh
cp .env.example .env     # fill in the API keys and a SESSION_SECRET
docker compose up --build
open http://localhost:8080    # create an account at /register
```

`db-roles` creates the database roles as the master user (see
[Stores](stores.md#database-roles)), then `migrate` runs `alembic upgrade head` as `aios_app`
before `api`/`worker` start, and `migrate-stores` brings the stores to the store tree's head
(see [Stores](stores.md#data-stores)). `rustfs` stands in for S3 (its S3 API on
http://localhost:9000, where download links send the browser), and `files-bucket` makes the
bucket. The worker image bakes in Docling's models and the chunker tokenizer at build time, so
it needs Hugging Face access while building but not at runtime.

### Debug with Claude Code

The `claude` service (compose profile `debug`) runs [Claude Code](https://code.claude.com) on the
stack's network, so an agent can debug with the same context you have: the repo (bind-mounted
at `/workspace`, so its edits land in your working copy), the database and Redis (`psql`,
`redis-cli`), the api/sandbox over HTTP, and every container's logs (`docker compose logs`).

```sh
docker compose up -d                              # the stack to debug
docker compose --profile debug run --rm claude    # interactive Claude Code session
docker compose --profile debug run --rm claude claude -p "why did the last ingest fail?"
```

- **Auth**: log in with `/login` on first run; it's kept in the `claude-home` volume. If `.env`
  has `ANTHROPIC_API_KEY`, Claude Code offers to use that key (API billing) instead.
- **Docker access is read-only**: it goes through `docker-proxy`, a socket proxy that allows
  logs, ps and inspect but no exec, restart or build. The agent tells you what to restart.
- **Context**: `infra/claude/CLAUDE.md` (installed as Claude Code's managed memory) describes
  the services and the rules above to the agent.
- The backend venv, caches and `frontend/node_modules` are volumes, so they don't clobber your
  host's. Run `uv sync` / `pnpm install` inside the container before running checks there.
- On Linux, files the agent writes are owned by uid 1000. If yours differs, build with
  `CLAUDE_UID=$(id -u) docker compose --profile debug build claude`.
- To upgrade Claude Code, bump `CLAUDE_CODE_VERSION` in `infra/claude/Dockerfile` (keep it
  at least a week old, per the [dependency policy](#dependency-policy)).

## Local development

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
uv run celery -A aios.worker worker --loglevel=INFO   # separate shell
uv run celery -A aios.worker worker -Q workflows --loglevel=INFO   # another, for workflow runs
uv run celery -A aios.worker worker -Q provisioning -B --loglevel=INFO   # and stores (-B: their retries)

cd frontend
pnpm install
pnpm dev                 # http://localhost:5173, proxies /api to :8000
```

### Seed data

aios isn't live, so a change to the schema keeps nothing for older rows: development data is
rebuilt instead. `make seed` (from the repository root; `backend/src/aios/seed.py`) migrates the
platform database to head, deletes every organisation's store (its database, roles and files)
and every row, then makes one working organisation through the API's own handlers: the user
`dev@example.com` (password `development only`) with a provisioned store, a skill, agents, a
workflow nested in another, a schedule, a public chat, and an archived agent. It refuses unless
`DEV_LOGIN_ENABLED` is on (pass `--force` otherwise), since it wipes the database.

## Checks and tests

- **Backend**: `uv run ruff check . && uv run ruff format --check . && uv run pytest`. DB tests
  need the migrated database: they skip when no server is listening or it isn't migrated, but
  fail when the server turns `DATABASE_URL` away (a missing role or database, a wrong password),
  so run `aios.db_roles` first. The role tests in `tests/test_db_roles.py` also need
  `DATABASE_ADMIN_URL`, from the environment or `.env`, and skip without it, and the documents
  tests need RustFS, at `DATA_STORE_FILES_ENDPOINT` with
  `DATA_STORE_FILES_ACCESS_KEY`/`_SECRET_KEY`, as in `backend/.env.example`; every test also
  needs Redis, at `REDIS_URL`, whose rate limits it resets (and through which a run's waiting
  turns hear its lease released). CI reaches stores through PgBouncer
  (`DATA_STORE_POOLER_ADDRESS`): [Testing](testing.md#through-the-pooler). The tests run in random order, and
  CI shards them: [Testing](testing.md) covers reproducing a seed and running one shard.
- **Infrastructure**: `terraform test` in `infra/terraform` (it plans and applies against a
  mocked AWS, so needs no account).
- **Frontend**: `pnpm build && pnpm test && pnpm test:e2e`. The browser tests (Playwright,
  `frontend/e2e/`) run the app against an in-memory fake of the API (`e2e/fake-api.ts`), so
  they need no backend. Run `pnpm exec playwright install chromium` once first, or point
  `PLAYWRIGHT_CHROMIUM_EXECUTABLE` at a Chromium you already have.
- **Browser test shards**: CI splits the browser tests across machines (the `browser` job in
  `.github/workflows/ci.yml`) and the full-stack test likewise (`fullstack-shard`); each shard
  count is the `shard:` line of its job's matrix (3 and 1). To run one shard locally, as CI does,
  from `frontend/`: `pnpm test:e2e --shard=2/3` (shard 2 of 3), or `pnpm test:fullstack
  --shard=1/1` against a running full-stack stack (`frontend/fullstack/README.md`). With `CI` set,
  each shard writes a blob report (`blob-report/`, or `blob-report-fullstack/`); the gate jobs
  (`frontend-gate`, `fullstack`) merge every shard's into one HTML report, uploaded as the
  `playwright-report` or `fullstack-report` artifact when a shard fails, and fail if any shard
  did. To merge locally, copy each shard's blob into one folder and run
  `pnpm exec playwright merge-reports --reporter html <folder>`.

## Dependency policy

- **Python**: every dependency in `pyproject.toml` is pinned with `==` and `uv.lock` is
  committed. `[tool.uv] exclude-newer = "7 days"` means `uv lock` only picks releases that are
  at least a week old. Upgrade with `uv lock --upgrade-package <name>` and update the pin.
- **Node**: `pnpm-workspace.yaml` sets `minimumReleaseAge: 10080`; `package.json` uses exact
  versions (`pnpm add --save-exact`).

## Changing models

`LLM_MODEL` takes any LangChain `init_chat_model` string ("provider:model"; other providers need
their `langchain-*` package). The tools and the UI stream adapter expect Anthropic's content
format, and web search is Anthropic's server-side tool, whose version is picked from the model
name (`llm.web_search_type`). `EMBEDDING_MODEL` is `voyageai:<model>`. The vector column is
`vector(1024)` (`EMBEDDING_DIM` in `datastores/models.py`), and embeddings are requested at
that size, so `EMBEDDING_MODEL` must support 1024-dimension output. To use a different size,
change `EMBEDDING_DIM`, add a store tree migration that changes the column size, then re-ingest
documents.

## Notes and trade-offs

- **Torch**: Docling needs `torch`, whose PyPI wheels pull in several GB of CUDA runtime.
  `pyproject.toml` locks torch/torchvision to PyTorch's CPU-only index instead, so installs
  (the worker image and a local `uv sync --extra ingest`) need access to `download.pytorch.org`
  and `download-r2.pytorch.org`.
- **Docker disk**: the worker image is still a few GB (torch CPU + Docling models). If a build
  fails with `read-only file system`, Docker's disk is full. Run `docker builder prune` and
  `docker system prune`, then restart Docker. On Docker Desktop, also raise the disk limit
  (Settings → Resources).

The old README's other notes are on the pages for their domains: uploaded files and the
one-store-per-organisation rule on [Stores](stores.md), agent runs, the checkpointer and inlined
skills on [Agents and skills](agents-and-skills.md), and the tools on [Tools](tools.md).
