# Brief for ticket-builder agents (Data stores stack, EarthlingOllie/aios)

You build ONE GitHub issue as one PR-sized branch in a stack. You build, test, review, and hand the review findings back to the orchestrator. You do NOT fix review findings yourself: the orchestrator dispatches one agent per finding.

## Repo and reading
- Backend: Python (uv) in `backend/`. Frontend: React/Vite (pnpm) in `frontend/`, Playwright e2e against an in-memory fake API (`frontend/e2e/`, `e2e/fake-api.ts`). Infra: `infra/terraform`, `infra/deploy.sh`, `docker-compose.yml`. README documents setup.
- Issue: GitHub MCP tools (ToolSearch `select:mcp__github__issue_read`), owner EarthlingOllie, repo aios.
- Plan: `git fetch origin claude/user-databases-plan && git show origin/claude/user-databases-plan:plan/user-databases/plan.md`.
- What's already built below you: #21 seam (`backend/src/aios/datastores/`: `data_store_for`, `store_session(id, role)`, `documents_session`, `store_owner`), #22 roles (`backend/src/aios/db_roles.py`: `aios_app`, `aios_provisioner`, template db `aios_store_template` with pgvector in an `extensions` schema), #23 provisioning (`datastores/provision.py`, store Alembic tree in `datastores/migrations/`, `migrate_all`, `retry_unfinished` via Celery beat in the provisioning worker, 409 `DataStoreNotReady` while not ready, `GET /api/data-store`, UI "being set up" state).

## Git
- Branch from the commit you're given; commit there; push with `git push -u origin <branch>` (retry on network errors with backoff 2/4/8/16s). Do NOT open a PR.
- Commit messages end with:
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01KC3fucdE9rANvvSzGLn7e5

## Local environment (another builder runs in parallel; use YOUR ports)
- Postgres 16 cluster of your own, port P, cluster name C (given below):
  - `pg_createcluster 16 C -p P --encoding UTF8 --locale C.UTF-8` (UTF8 matters: SQL_ASCII breaks db_roles)
  - `sed -i 's/^local\s\+all\s\+postgres\s\+peer/local all postgres trust/' /etc/postgresql/16/C/pg_hba.conf`
  - `pg_ctlcluster 16 C start`
  - `psql -h /var/run/postgresql -p P -U postgres -c "CREATE ROLE aios LOGIN SUPERUSER PASSWORD 'aios'" -c 'CREATE DATABASE aios_C OWNER aios'`
  - pgvector 0.8.1 is installed for PG16.
- Redis of your own: `redis-server --port R --daemonize yes --dir /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad` (the --dir keeps dump.rdb out of the repo).
- Env, exported before backend commands (run from `backend/`):
  DATABASE_ADMIN_URL=postgresql+psycopg://aios:aios@localhost:P/aios_C
  DATABASE_URL=postgresql+psycopg://aios_app:aios_app@localhost:P/aios_C
  DATA_STORE_PROVISIONER_URL=postgresql+psycopg://aios_provisioner:aios_provisioner@localhost:P/aios_C
  REDIS_URL=redis://localhost:R/0
  (don't set SCHEDULER_ENABLED=false)
- Backend: `uv run python -m aios.db_roles && uv run alembic upgrade head && uv run python -m aios.datastores.provision migrate-all && uv run pytest -q -p no:cacheprovider`; lint `.venv/bin/ruff check . && .venv/bin/ruff format --check .`; `uv run alembic check`.
- Known pre-existing bug, not yours unless your ticket touches it: with all extras installed (docling present), `tests/test_ingest.py::test_ingest_tolerates_document_deleted_mid_ingest` errors in teardown (`db.expunge_all()` detaches the `user` fixture), leaving a `test-user` row that breaks ~140 later tests. Without docling, test_ingest.py is skipped. If your ticket rewrites ingest, fix it there.
- Frontend (in `frontend/`): `pnpm install --frozen-lockfile`, `pnpm typecheck && pnpm test && pnpm build`, e2e: `E2E_PORT=<given> PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium-1194/chrome-linux/chrome pnpm exec playwright test`. Never run `playwright install`.
- Real-app Playwright drive: copy the harness from /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds23/drive/ (run.sh, serve.py, drive.cjs, start-worker.sh, reset.py) into your own evidence dir and adapt ports/paths. It fakes GitHub sign-in, Docling and Voyage. Don't edit the original.
- Docker Hub rate-limits; pull through mirror.gcr.io if you need images. Terraform: CI's 1.16.5 binary and providers are in /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/tfbin and tfrc (`TF_CLI_CONFIG_FILE=.../scratchpad/tfrc`, own `TF_DATA_DIR`, `init -backend=false`, `validate`, then delete the generated `.terraform.lock.hcl`).
- When done: stop and drop your cluster, shut down your Redis.

## Steps
1. Build the ticket test-first: invoke the Skill tool with skill `mattpocock-skills:tdd` and follow it until every acceptance criterion is met. Match the surrounding code's conventions and comment style. Don't widen scope beyond the ticket.
2. Run full backend, frontend unit and Playwright e2e suites; drive the real app with Playwright for every user-visible behaviour of the ticket. Fix anything found. Commit, push.
3. Code review of your branch against your base commit: invoke the Skill tool with skill `code-review` (args `<base sha> high`); fall back to `mattpocock-skills:code-review`. Make sure it reviews only your commits.
4. Push, then return: what was built; test/Playwright results with counts; evidence paths (under /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/<your dir>/); pushed head sha; anything you couldn't verify; and the full list of review findings, each with file:line, description and a concrete failure scenario.

## Update: #24 and #26 are built (read this too)
- #24 `claude/data-stores-24-documents` (head 56584b0): documents and chunks live in each store (`aios.documents`/`aios.chunks`, models in `datastores/models.py` on `StoreBase`, store revision `0002_documents`); the platform tables are dropped (0013). `store_session(id, "rw"|"ro")` and `store_engine(id, role)` open sessions on the store's own database; `DataStoreBusy` → 503 (role connection limit or a full per-process pool); `retry_unfinished` (10-min Celery beat in the provisioning worker) also brings ready stores behind head up to it; `migrate_store` re-applies role limits.
- #26 `claude/data-stores-26-query` (head c3bccf1): `describe_data`/`query_data` in `tools/data.py`, store revision `0002_data_schema` (an empty `data` schema), per-status model messages in `datastores/notices.py` (`store_not_ready(status)`), `provision.drop` that ends own-role sessions and does a plain DROP.
- The two branches have NOT been merged with each other yet (both add a store revision numbered 0002, and both touch `connections.py`, `provision.py` and search). An integration branch is being made separately. Build on the base you're given and don't try to merge the other branch in.
- Test env: DATABASE_ADMIN_URL must be set, or ~20 role/provisioning tests skip silently. A correct full run skips 1 (test_ingest without docling). On a brand-new database, a single agent-turn test run alone can hang on the checkpointer's `CREATE INDEX CONCURRENTLY`; create its tables first (see /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds26/f1/scripts/setup_checkpointer.py) or run the full suite. A killed run can leave a test user and a `ds_*` database behind; clean them up before trusting the next run. Known flaky: `test_sandbox.py::test_egress_proxy_refusals[CONNECT other.com…]`.
- Usage limits have interrupted agents twice. Commit work-in-progress often so a resume loses little.
