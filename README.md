# aios

Chat with your documents. Upload files, they're parsed and chunked with Docling, embedded with
Voyage, and stored in Postgres/pgvector. Chat uses a LangGraph agent (LangChain's
`create_agent`, on Anthropic) that calls a hybrid search tool (vector + full-text, fused with
Reciprocal Rank Fusion).

You can also define your own **agents** (a base prompt plus a set of **skills**, which are
reusable instructions, and **tools** from a built-in library: web search, and document search,
the same hybrid search as the documents chat, over the documents of the user running the agent)
and chat with them. The **Search** page runs that hybrid search directly, with no agent in
between, and shows each passage with how the semantic and keyword branches ranked it. Each
conversation with an agent is saved as a **run** (a LangGraph thread), whose page shows the
conversation and an event log: messages, model responses, tool calls and results, the
instructions sent and token usage.

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
- Each run is traced to Langfuse when it's configured (see [Tracing](#tracing)), with
  `agent_id`, `agent_version`, `agent_version_id`, the skill versions and the tools with their
  settings as trace metadata, and the run's thread id, `agent-run-<id>`, as the Langfuse
  session.

**Workflows** graph agents together, and can run other workflows as subgraphs (`/api/workflows`).
The Workflows page draws each one as a graph (React Flow, laid out with dagre); clicking a node or
an edge shows its configuration. Its editor adds nodes for your agents and workflows, connects
them by dragging between nodes, and removes, renames or re-points them, refusing edges that would
make a cycle before you save. Runs are started from the workflow's page; a run's page draws the
path it took on the graph (which nodes succeeded, failed or never ran, and the edges their
outputs went along) and, while it's going, polls to show where it is now. Clicking a node shows
its step's input, output and conversation, and a subworkflow node opens its own graph. They're versioned like agents (`workflows`,
`workflow_versions`):

- A version is a directed acyclic graph: nodes (`workflow_nodes`), each pinning one agent version
  or one workflow version by id, and edges (`workflow_edges`) between node keys. Nodes with no
  incoming edge are where the workflow starts.
- Changing the graph (nodes, edges, or which version a node pins) creates a new version; renaming,
  moving nodes in the editor (the workflow's `layout`, positions by node key) and no-op saves
  don't. Editing an agent doesn't change any workflow: the node reports its
  latest version, and taking it is a new workflow version. Restore works as for agents.
- A workflow can't contain itself, directly or through other workflows at any depth: none of the
  versions a new version pins, nor any they pin in turn, may belong to it. Nodes can only pin
  versions that already exist, so nesting is always finite.
- Agents and workflows are soft-deleted too: older workflow versions keep pinning their versions,
  and stay readable (a node whose agent or workflow is gone shows no latest version). One can't
  be deleted while a live workflow's current version runs it; older versions don't count, but
  restoring one that runs something since deleted is refused. Deleting an agent still deletes
  its runs, their threads and sandboxes. Names are unique among live rows, so they can be reused.
- Saves and deletes lock the rows involved (skills, then agents, then workflows, each by id), so a
  delete can't race a save that starts using what it deletes.
- A **run** (`workflow_runs`) is given one text input and pins the version that's current when it
  starts. A worker of its own runs it (`aios.run_workflow`, a Celery task on the `workflows`
  queue, so ingests don't wait behind runs, with its own time limit, `WORKFLOW_RUN_TIME_LIMIT`),
  refreshing its heartbeat (`heartbeat_at`) as it goes; a run whose heartbeat has stopped (its
  worker died) is failed when it's next read or its task comes round again, and one whose
  worker's event loop has made no progress for `WORKFLOW_RUN_NO_PROGRESS_LIMIT` (5 minutes; stuck
  in a blocking call) is failed by the worker itself, while a node awaiting a slow model or tool
  call isn't. A node starts once
  all its predecessors have succeeded, so independent branches run at the same time; start nodes
  get the run's input, other nodes their predecessors' outputs in edge order, joined with a blank
  line. An agent node is a new agent run of its pinned version, given its input as the user's
  message, and outputs the final reply, so its conversation opens on the run page like any other.
  A workflow node runs its pinned version's graph with its input, and outputs its sink nodes'
  outputs (in node order); so does the run. Each node records a step (`workflow_run_steps`, with
  the path of node keys down to it) when it starts and finishes, committed as it goes, so the run
  can be watched while it's going. Once a node fails nothing else starts, and the run fails with
  the first error. Each agent node is traced like an agent run, with the workflow, its version,
  the run and the node's path added to the metadata.
- Starting a run is refused if any agent version in the graph, at any depth, couldn't run (a tool
  that has left the library, or an agent that has since been deleted), or if it has no nodes.
  Deleting a run deletes its agent runs; deleting the workflow leaves its runs in the database.

**Triggers** start agents and workflows without a user in the loop. Each has a kind, and the
first is a **schedule**: a cron expression (5 fields, or `@hourly`, `@daily`, `@weekly`,
`@monthly`, `@yearly`) in an IANA timezone. The **Scheduler** page lists every schedule, the
next to run first, and creates, edits, pauses and deletes them; each agent's and workflow's page
does the same for its own (`/api/triggers`).

- An agent's schedule carries a message. Each time it fires, a new run of the agent's *current*
  version starts with that message as the user's, and runs one turn exactly as a chat turn does
  (the run is listed with the agent's others, marked as scheduled). The schedule shows how its
  last run went, with a link to it.
- Workflows can't run yet, so a workflow's schedule records a failed run each time it fires.
- Each API process runs the scheduler (`aios.scheduler`), polling every
  `SCHEDULER_INTERVAL_SECONDS` (15). Due schedules are claimed with `FOR UPDATE SKIP LOCKED`
  and moved on to their next run in the same transaction, so with any number of replicas each
  run fires once. Set `SCHEDULER_ENABLED=false` to keep a process from running them.
- Missed runs aren't caught up: a schedule that's overdue (nothing was running) fires once, then
  carries on from now. Across daylight-saving changes it behaves like cron: a skipped local time
  runs when the clock jumps, and a repeated one runs once. A turn still running when the process
  stops keeps what it got to, and its schedule records that it failed.
- Deleting an agent or a workflow deletes its schedules. Cron parsing is `cronsim`'s, wrapped in
  `aios.cron`.

```
browser ──▶ ALB / nginx ──┬─ /api/*, /login, /logout, /auth/* ──▶ api (FastAPI + Uvicorn)
                          └─ everything else ─────────────────▶ web (React SPA, static)
api ──enqueue──▶ Redis ──┬─▶ worker (Celery: Docling ▶ HybridChunker ▶ Voyage embeddings)
                         └─▶ workflow-worker (Celery, `workflows` queue: workflow runs; api image)
api, worker ──▶ Postgres 17 + pgvector (agent run history: LangGraph checkpoints, `langgraph` schema)
api, worker ──traces──▶ Langfuse (optional, self-hosted)
```

| Layer | Stack |
| --- | --- |
| Backend | Python 3.13, uv (exact pins, `exclude-newer = "7 days"`), hatchling, FastAPI + Uvicorn, Celery, pydantic-settings, SQLAlchemy + Alembic, psycopg 3 |
| Auth | Authlib GitHub OAuth; Starlette `SessionMiddleware` (itsdangerous-signed cookie) holds the OAuth state, then the user id |
| Frontend | React + TypeScript, Vite, pnpm (`minimumReleaseAge: 10080` = 7 days), Tailwind v4 + shadcn/ui, Vercel AI SDK `useChat`, react-markdown + remark-gfm; markdown editing via `@github/markdown-toolbar-element` on plain textareas |
| Retrieval | pgvector HNSW (cosine) + Postgres FTS (`tsvector`, GIN), RRF (k=60) |
| Inference | LangGraph agents (LangChain `create_agent`, `anthropic:claude-sonnet-5-5` via `langchain-anthropic`), streamed to the UI by our own Vercel AI SDK adapter (`ui_stream.py`); LangGraph Postgres checkpointer for run history; `langchain-voyageai` embeddings (`voyageai:voyage-4`, 1024-d); Docling `HybridChunker` |
| Tracing | Langfuse (open source, self-hosted) via its LangChain callback handler |
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
    api/workflows.py CRUD /api/workflows, versions and restore, runs: /api/workflows/{id}/runs
    api/triggers.py CRUD /api/triggers (schedules), schedule preview, timezones
    scheduler.py    fires due schedules (runs in each API process)
    cron.py         cron expressions in a timezone (cronsim)
    workflows.py    workflow graph validation, and running workflows
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
    agent.py        the documents chat's agent + search_documents tool, and the run context
    search.py       hybrid search SQL (RRF)
    ingest.py       Docling convert → HybridChunker → embed → store
    worker.py       Celery app + ingest and workflow run tasks
  sandbox/          the sandbox image's pinned libraries (requirements.in/.txt)
  Dockerfile        targets: api, worker, sandbox
frontend/           Vite React SPA (+ nginx images/configs)
infra/terraform/    AWS
infra/deploy.sh     build → push → migrate → roll out
docker-compose.yml  db, redis, migrate, api, worker, workflow-worker, web, sandbox
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
uv run celery -A aios.worker worker -Q workflows --loglevel=INFO   # another, for workflow runs

cd frontend
pnpm install
pnpm dev                 # http://localhost:5173, proxies /api, /login, /auth to :8000
```

Checks: `uv run ruff check . && uv run ruff format --check . && uv run pytest` (backend; DB tests
need the migrated database and skip otherwise) and `pnpm build && pnpm test && pnpm test:e2e`
(frontend). The browser tests (Playwright, `frontend/e2e/`) run the app against an in-memory fake of the API (`e2e/fake-api.ts`), so they
need no backend. Run `pnpm exec playwright install chromium` once first, or point
`PLAYWRIGHT_CHROMIUM_EXECUTABLE` at a Chromium you already have.

### Dependency policy

- **Python**: every dependency in `pyproject.toml` is pinned with `==` and `uv.lock` is
  committed. `[tool.uv] exclude-newer = "7 days"` means `uv lock` only picks releases that are
  at least a week old. Upgrade with `uv lock --upgrade-package <name>` and update the pin.
- **Node**: `pnpm-workspace.yaml` sets `minimumReleaseAge: 10080`; `package.json` uses exact
  versions (`pnpm add --save-exact`).

### Changing models

`LLM_MODEL` takes any LangChain `init_chat_model` string ("provider:model"; other providers need
their `langchain-*` package). The tools and the UI stream adapter expect Anthropic's content
format, and web search is Anthropic's server-side tool, whose version is picked from the model
name (`llm.web_search_type`). `EMBEDDING_MODEL` is `voyageai:<model>`. The vector column is
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
  for development only. Allowed domains need no egress proxy there (`SANDBOX_PROXY_*` unset): the
  runner can reach every domain anyway.

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
  saved yet. Agent runs are different: each run is a LangGraph thread (`agent-run-<id>`) kept by
  the Postgres checkpointer, the client sends only its new message, and the agent continues
  from the thread's latest checkpoint. The user's message is checkpointed before the model is
  called; if a turn fails or is stopped, the reply's text so far (or an error result for each
  tool call that hadn't finished) is added to the thread, so the next turn sees what the user
  saw. Turns of the same run are serialized by a Postgres advisory lock: a second tab's turn
  waits for the first, then builds on it. `agent_runs` keeps a message count and title for
  listings.
- **The checkpointer's tables are LangGraph's, not Alembic's.** They live in the `langgraph`
  schema, created and migrated by `AsyncPostgresSaver.setup()` (under an advisory lock) the
  first time a process runs a turn. Alembic only compares the `public` schema, so `alembic
  check` doesn't see them. Deleting a run or agent deletes its threads too (they aren't foreign
  keys, so this is done explicitly). The API's database role needs to be able to create that
  schema.
- **Tools are code, not data.** Each lives in `backend/src/aios/tools/`, registered under a stable
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

## Tracing

Agent runs and the documents chat are traced to [Langfuse](https://langfuse.com) (open source,
self-hostable) through its LangChain callback handler, when both keys are set:

```sh
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://langfuse.example.com   # your instance; default http://localhost:3000
LANGFUSE_ENVIRONMENT=production                  # optional
```

Each trace carries the user id, the run's thread id as the Langfuse session, tags for the agent
and version, and the run metadata above. With no keys, nothing is traced. Traces are sent in the
background and flushed when the API shuts down.
