# aios

Chat with your documents. Upload files, they're parsed and chunked with Docling, embedded with
Voyage, and stored in Postgres/pgvector. Chat uses a Pydantic AI agent (Anthropic) that calls a
hybrid search tool (vector + full-text, fused with Reciprocal Rank Fusion).

You can also define your own **agents** (a base prompt plus a set of **skills**, which are
reusable instructions, and **tools** from a built-in library: web search, and document search,
the same hybrid search as the documents chat, over the documents of the user running the agent)
and chat with them. Each conversation with an agent is saved as a **run**, whose page shows the conversation
and an event log: model requests and responses, tool calls, the instructions sent and token
usage.

Agents and skills are **versioned**. Each has an editable identity row (name, description) and a
chain of immutable versions (`agent_versions`, `skill_versions`) holding everything that affects
the model's behaviour:

- Changing an agent's prompt, skills or tools (or a tool's settings) creates a new version.
  Renaming doesn't, and nor does saving without changes (configs are compared by hash).
- Changing a skill's description or content creates a new version, and so does renaming it: the
  name is sent to the model in each agent's instructions, so each skill version snapshots it.
- Each agent version pins specific skill versions and stores the exact rendered instructions.
  Editing a skill doesn't change any agent; the agent shows "Update to vN" and taking the update
  creates a new agent version.
- A run is pinned to the agent version it started on, so editing an agent mid-conversation
  doesn't change it.
- Restoring an old version creates a new one with that config; numbers only go up.
- Skills are soft-deleted (old agent versions still reference their versions), and can't be
  deleted while an agent's current version uses them.
- Each run passes `agent_id`, `agent_version`, `agent_version_id`, the skill versions and the
  tools with their settings as Pydantic AI run metadata, and `agent-run-<id>` as the
  conversation id. These reach OpenTelemetry spans once instrumentation is set up (e.g. for
  Langfuse).

```
browser ──▶ ALB / nginx ──┬─ /api/*, /login, /logout, /auth/* ──▶ api (FastAPI + Uvicorn)
                          └─ everything else ─────────────────▶ web (React SPA, static)
api ──enqueue──▶ Redis ──▶ worker (Celery: Docling ▶ HybridChunker ▶ Voyage embeddings)
api, worker ──▶ Postgres 17 + pgvector
```

| Layer | Stack |
| --- | --- |
| Backend | Python 3.13, uv (exact pins, `exclude-newer = "7 days"`), hatchling, FastAPI + Uvicorn, Celery, pydantic-settings, SQLAlchemy + Alembic, psycopg 3 |
| Auth | Authlib GitHub OAuth; Starlette `SessionMiddleware` (itsdangerous-signed cookie) holds the OAuth state, then the user id |
| Frontend | React + TypeScript, Vite, pnpm (`minimumReleaseAge: 10080` = 7 days), Tailwind v4 + shadcn/ui, Vercel AI SDK `useChat` |
| Retrieval | pgvector HNSW (cosine) + Postgres FTS (`tsvector`, GIN), RRF (k=60) |
| Inference | Pydantic AI agent (`anthropic:claude-sonnet-5-5`) streaming via its Vercel AI adapter; Pydantic AI `Embedder` (`voyageai:voyage-4`, 1024-d); Docling `HybridChunker` |
| Infra | Docker Compose locally; Terraform on AWS: VPC, ECS Fargate, ALB, RDS Postgres, ElastiCache (Valkey), ECR, Secrets Manager |

## Layout

```
backend/            FastAPI app, Celery worker, Alembic migrations, tests
  src/aios/
    main.py         app + SessionMiddleware
    auth.py         /login, /auth/callback, /logout, current_user
    api/chat.py     POST /api/chat (AI SDK UI message stream)
    api/documents.py upload/list/delete, GET /api/search
    api/skills.py   CRUD /api/skills
    api/agents.py   CRUD /api/agents, runs: /api/agents/{id}/runs[/{run_id}[/chat]]
    runner.py       the Pydantic AI agent that runs user-defined agents
    versioning.py   creating agent/skill versions, rendering instructions
    tools/          the tool library agents can be given (registry + one module per tool)
    api/tools.py    GET /api/tools (the library, with each tool's settings schema)
    agent.py        Pydantic AI agent + search_documents tool
    search.py       hybrid search SQL (RRF)
    ingest.py       Docling convert → HybridChunker → embed → store
    worker.py       Celery app + ingest task
  Dockerfile        targets: api, worker
frontend/           Vite React SPA (+ nginx images/configs)
infra/terraform/    AWS
infra/deploy.sh     build → push → migrate → roll out
docker-compose.yml  db, redis, migrate, api, worker, web
```

## Prerequisites

1. A **GitHub OAuth app** (Settings → Developer settings → OAuth Apps). Callback URL:
   `<PUBLIC_URL>/auth/callback`, e.g. `http://localhost:8080/auth/callback` for Compose,
   `http://localhost:5173/auth/callback` for `pnpm dev`.
2. An **Anthropic** API key and a **Voyage AI** API key.

## Run everything in Docker

```sh
cp .env.example .env     # fill in GitHub + API keys and a SESSION_SECRET
docker compose up --build
open http://localhost:8080
```

`migrate` runs `alembic upgrade head` before `api`/`worker` start. The worker image bakes in
Docling's models and the chunker tokenizer at build time, so it needs Hugging Face access
while building but not at runtime.

## Local development

```sh
docker compose up -d db redis

cd backend
cp .env.example .env     # PUBLIC_URL=http://localhost:5173
uv sync --extra ingest   # drop --extra ingest if you're not running the worker
uv run alembic upgrade head
uv run uvicorn aios.main:app --reload
uv run celery -A aios.worker worker --loglevel=INFO   # separate shell

cd frontend
pnpm install
pnpm dev                 # http://localhost:5173, proxies /api, /login, /auth to :8000
```

Checks: `uv run ruff check . && uv run ruff format --check . && uv run pytest` (backend; DB tests
need the migrated database and skip otherwise) and `pnpm build` (frontend).

### Dependency policy

- **Python**: every dependency in `pyproject.toml` is pinned with `==` and `uv.lock` is
  committed. `[tool.uv] exclude-newer = "7 days"` means `uv lock` only picks releases that are
  at least a week old. Upgrade with `uv lock --upgrade-package <name>` and update the pin.
- **Node**: `pnpm-workspace.yaml` sets `minimumReleaseAge: 10080`; `package.json` uses exact
  versions (`pnpm add --save-exact`).

### Changing models

`LLM_MODEL` and `EMBEDDING_MODEL` take any Pydantic AI model string. The vector column is
`vector(1024)` (`EMBEDDING_DIM` in `models.py`), and embeddings are requested at that size, so
`EMBEDDING_MODEL` must support 1024-dimension output. To use a different size, change
`EMBEDDING_DIM`, add a migration that changes the column size, then re-ingest documents.

## Deploy to AWS

```sh
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # region, optional domain + ACM cert
terraform init
terraform apply                                 # VPC, RDS, Valkey, ALB, ECS, ECR, secrets

# Credentials stay out of Terraform state. Set them once:
aws secretsmanager put-secret-value --secret-id aios/external --secret-string '{
  "GITHUB_CLIENT_ID": "...", "GITHUB_CLIENT_SECRET": "...",
  "ANTHROPIC_API_KEY": "...", "VOYAGE_API_KEY": "..."}'

cd ../..
infra/deploy.sh          # build & push images, run migrations, roll out (tag = git sha)
```

Services won't become healthy until the first `deploy.sh` has pushed images. Everything runs
in one region (`var.region`). RDS and Valkey sit in private subnets and only accept traffic
from the app security group. Without `certificate_arn` the site is served over plain HTTP on
the ALB hostname and session cookies aren't marked `Secure`. Set a domain and certificate for
anything real.

## Notes and trade-offs

- **Uploads are stored in Postgres** (`documents.data`), so the API and worker don't need a
  shared bucket. Move them to S3 if files get large.
- **Torch**: Docling needs `torch`, whose PyPI wheels pull in several GB of CUDA runtime.
  `pyproject.toml` locks torch/torchvision to PyTorch's CPU-only index instead, so installs
  (the worker image and a local `uv sync --extra ingest`) need access to `download.pytorch.org`
  and `download-r2.pytorch.org`.
- **Docker disk**: the worker image is still a few GB (torch CPU + Docling models). If a build
  fails with `read-only file system`, Docker's disk is full. Run `docker builder prune` and
  `docker system prune`, then restart Docker. On Docker Desktop, also raise the disk limit
  (Settings → Resources).
- Document chat history is client-side (`useChat` sends the conversation each turn). It isn't
  saved yet. Agent runs are different: the server stores each run's full Pydantic AI message
  history (`agent_runs.messages`, JSONB), the client sends only its new message, and the server
  replays the stored history into the next turn.
- **Tools are code, not data.** Each lives in `backend/src/aios/tools/`, registered under a stable
  key with a Pydantic settings model (every field has a default, and its JSON schema drives the
  settings form). An agent version stores which tools it has and their validated settings
  (`agent_version_tools`); each run builds them and passes them to Pydantic AI as capabilities.
  To add a tool, write a module that calls `register(ToolSpec(...))` and import it in
  `aios/tools/__init__.py`. Never rename a key: old versions reference it. If a tool's behaviour
  changes in a way old versions shouldn't get, register it under a new key. A version whose tool
  has been removed from the library can still be viewed, but not restored or run.
- **Web search** uses the model provider's native search tool (Anthropic's server-side web
  search), which is billed per search; its settings cap searches per model request and can
  allow or block domains. A model without native web search fails the turn rather than running
  without it.
- **Skills are inlined into the agent's instructions** (base prompt, then each pinned skill's
  description and instructions in a `<skill>` block). That's simple and works well for a handful
  of skills. With many large skills, switch to progressive disclosure: list only names and
  descriptions, and give the agent a tool to load a skill's full instructions.
- Agents have no tools yet; the run UI and event log already render tool calls and returns.
