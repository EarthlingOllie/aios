# Debugging the aios dev stack

You are running in the `claude` container of the aios `docker compose` project, on the same
network as the running stack. The repo is bind-mounted at `/workspace`: edits you make there
change the developer's working copy directly. Read `/workspace/README.md` for the architecture.

## Services (hostnames on this network)

| Service           | Address                     | Notes                                           |
| ----------------- | --------------------------- | ----------------------------------------------- |
| `api`             | `http://api:8000`           | FastAPI; `/api/...`                             |
| `web`             | `http://web:80`             | nginx serving the SPA, proxies to `api`         |
| `db`              | `db:5432`                   | Postgres + pgvector, user/password/db `aios`    |
| `redis`           | `redis:6379`                | Celery broker                                   |
| `sandbox`         | `http://sandbox:8080`       | `run_python` runner (token `dev-sandbox-token`) |
| `worker`          | -                           | Celery, ingest queue (Docling)                  |
| `workflow-worker` | -                           | Celery, `workflows` queue                       |

`DATABASE_URL`, `REDIS_URL` and the `SANDBOX_*` variables are set as they are for the api, so
backend code and scripts run here talk to the live stack.

## Looking at the stack

- Logs: `docker compose logs --tail=200 api` (or `worker`, `workflow-worker`, `migrate`, ...),
  `docker compose ps`. Run these from `/workspace`.
- Docker access is **read-only** (a socket proxy): logs, ps, inspect work; restart, exec, up
  and build don't. When a container needs restarting or rebuilding to pick up a change, say so
  and give the developer the exact command (e.g. `docker compose up -d --build api`).
- Database: `psql postgresql://aios:aios@db:5432/aios`. Redis: `redis-cli -h redis`.
- HTTP: `curl -s http://api:8000/...`. Most endpoints need a session cookie from GitHub login.

## Running code and checks

- Backend (`cd /workspace/backend`): `uv sync --extra ingest` first (the venv is at
  `/home/dev/.venv`, outside the repo), then `uv run pytest`, `uv run ruff check .`,
  `uv run alembic ...`. Tests that touch the DB use the live database: don't run destructive
  migrations (`alembic downgrade`) or wipe tables without asking.
- Frontend (`cd /workspace/frontend`): `pnpm install --frozen-lockfile`, then `pnpm build`,
  `pnpm test`. Playwright browsers aren't installed here.

Prefer finding the root cause from logs, data and code over guessing; report what you found
and what you changed.
