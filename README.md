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
    sandbox/        sandboxes for the run_python tool: sessions, backends, the in-sandbox
                    runner and the egress proxy
    agent.py        Pydantic AI agent + search_documents tool
    search.py       hybrid search SQL (RRF)
    ingest.py       Docling convert → HybridChunker → embed → store
    worker.py       Celery app + ingest task
  sandbox/          the sandbox image's pinned libraries (requirements.in/.txt)
  Dockerfile        targets: api, worker, sandbox
frontend/           Vite React SPA (+ nginx images/configs)
infra/terraform/    AWS
infra/deploy.sh     build → push → migrate → roll out
docker-compose.yml  db, redis, migrate, api, worker, web, sandbox
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

### Sandboxes on Fargate (`run_python`)

On AWS, `SANDBOX_BACKEND=fargate` (`aios.sandbox.fargate`, `infra/terraform/sandbox.tf`): each
agent run that uses `run_python` gets its own Fargate task, so its own Firecracker microVM.

- **Starting**: the API calls `ecs:RunTask` on the sandbox task definition, passing the session's
  runner token, proxy URL and lifetimes as environment overrides, waits for the task to run and
  calls the runner (`serve` mode) at the task's private IP on port 8080. The runner exits when
  idle or at its maximum lifetime, which stops the task; the API also stops it if starting fails.
- **Network**: sandboxes run in subnets of their own whose route table has no default route.
  Their security group only admits the API (8080) and only lets them out to the egress proxy
  (3128), plus the ECR, CloudWatch Logs and S3 VPC endpoints Fargate needs to pull the image and
  ship logs. The S3 endpoint only allows ECR's layer bucket, and the interface endpoints only
  this account's credentials. RDS and Valkey don't admit sandboxes. The proxy is an ECS service
  (`egress-proxy`, API image) found at `proxy.<name>.internal` through Cloud Map.
- **IAM**: the sandbox task has no task role, so code in it has no AWS credentials. The API's
  task role (its own: the worker, web and migrate tasks share one with no permissions) may run
  only the sandbox task definition, in this cluster, and describe and stop the cluster's tasks.
- **Locally**, `docker compose` runs one `sandbox` container that every run shares
  (`SANDBOX_BACKEND=dev`). It has no isolation between runs and no network restrictions, so it's
  for development only.

Known limits:

- A sandbox takes tens of seconds to start. Starting it in the background when a turn starts
  hides most of that, but not all of it on a run's first turn.
- Each sandbox is one `ecs:RunTask` call (rate limited per account) and one IP in the sandbox
  subnets (a `/24` per AZ, so about 250 concurrent sandboxes per AZ).
- The runner token is a RunTask override, visible to anyone in the account with
  `ecs:DescribeTasks`.
- DNS lookups through the VPC resolver aren't filtered, so sandbox code can leak data in the
  names it looks up, and receive data in the answers. Route 53 DNS Firewall
  can't simply allowlist the names sandboxes need: its rule groups apply to a whole VPC, and
  the API, worker and egress proxy share this one and resolve any name (the proxy resolves
  whatever domains a run allows). Closing this means moving sandboxes into a VPC of their own
  (with their own endpoints, and the proxy and runner reached across VPCs), whose DNS Firewall
  allows only the proxy's Cloud Map name and the ECR, S3 and CloudWatch Logs endpoint names.
- The three interface endpoints cost about $45 a month across two AZs, plus data processing.

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
  has been removed from the library can still be viewed, but not restored or run. Stored
  settings are loaded leniently: fields the settings model has since dropped are ignored and new
  fields take their defaults, so changing a settings model doesn't break old versions. Saving,
  restoring and running all load them the same way, and refuse a value the model now rejects.
- **Web search** uses the model provider's native search tool (Anthropic's server-side web
  search), which is billed per search; its settings cap searches per model request and can
  allow or block domains. A model without native web search fails the turn rather than running
  without it.
- **Run Python** (`run_python`) lets an agent write and run Python in a sandbox: one per agent
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
  available. See `plan/run-python/plan.md`.
- **Skills are inlined into the agent's instructions** (base prompt, then each pinned skill's
  description and instructions in a `<skill>` block). That's simple and works well for a handful
  of skills. With many large skills, switch to progressive disclosure: list only names and
  descriptions, and give the agent a tool to load a skill's full instructions.
- Agents have no tools yet; the run UI and event log already render tool calls and returns.
