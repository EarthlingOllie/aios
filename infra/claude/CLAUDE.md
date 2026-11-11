# Debugging the aios dev stack

You are running in the `claude` container of the aios `docker compose` project, on the same
network as the running stack. The repo is bind-mounted at `/workspace`: edits you make there
change the developer's working copy directly. Read `/workspace/README.md` for the architecture,
and `/workspace/docs/` for how each part works (one page per domain, listed in
`/workspace/docs/README.md`).

## Services (hostnames on this network)

| Service           | Address                     | Notes                                           |
| ----------------- | --------------------------- | ----------------------------------------------- |
| `api`             | `http://api:8000`           | FastAPI; `/api/...`                             |
| `web`             | `http://web:80`             | nginx serving the SPA, proxies to `api`         |
| `db`              | `db:5432`                   | Postgres + pgvector (database `aios`)           |
| `redis`           | `redis:6379`                | Celery broker                                   |
| `rustfs`          | `http://rustfs:9000`        | S3 stand-in for uploaded files (`rustfsadmin`)  |
| `sandbox`         | `http://sandbox:8080`       | `run_python` runner (token `dev-sandbox-token`) |
| `run-service`     | -                           | Agent turns, handoffs, schedules (work queue)   |
| `worker`          | -                           | Celery, ingest queue (Docling)                  |
| `workflow-worker` | -                           | Celery, `workflows` queue                       |
| `provisioning-worker` | -                       | Celery, `provisioning` queue: data stores       |

One-shot setup services, whose logs explain a stack that didn't start: `db-roles`,
`files-bucket`, `migrate`, `migrate-stores`.

`DATABASE_URL`, `REDIS_URL`, the `DATA_STORE_FILES_*` and the `SANDBOX_*` variables are set as
they are for the api, so backend code and scripts run here talk to the live stack, as the app's
role. You don't have the provisioner's credentials (`DATA_STORE_PROVISIONER_URL`).

## Looking at the stack

- Logs: `docker compose logs --tail=200 api` (or `worker`, `workflow-worker`, `migrate`, ...),
  `docker compose ps`. Run these from `/workspace`.
- Docker access is **read-only** (a socket proxy): logs, ps, inspect work; restart, exec, up
  and build don't. When a container needs restarting or rebuilding to pick up a change, say so
  and give the developer the exact command (e.g. `docker compose up -d --build api`).
- Database: `psql postgresql://aios_app:aios_app@db:5432/aios` (the app's role, which owns
  the platform schema). The master user (`aios:aios`) is for break-glass only; use it just to
  read what the app's role can't, such as a user's data store database, and never to change
  roles or ownership. Redis: `redis-cli -h redis`.
- HTTP: `curl -s http://api:8000/...`. Most endpoints need a session cookie: sign in with
  `POST /api/auth/login` (`{"email": ..., "password": ...}`) and keep its cookie.

## Running code and checks

- Backend (`cd /workspace/backend`): `uv sync --extra ingest` first (the venv is at
  `/home/dev/.venv`, outside the repo), then `uv run pytest`, `uv run ruff check .`,
  `uv run alembic ...`. Tests that touch the DB use the live database: don't run destructive
  migrations (`alembic downgrade`) or wipe tables without asking.
- Frontend (`cd /workspace/frontend`): `pnpm install --frozen-lockfile`, then `pnpm build`,
  `pnpm test`. Playwright browsers aren't installed here.

Prefer finding the root cause from logs, data and code over guessing; report what you found
and what you changed.
