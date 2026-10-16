# Brief for #34 review-finding agents

You investigate ONE code-review finding on branch `claude/data-stores-34-history-export` (head 371f4f1) of EarthlingOllie/aios, which builds issue #34 "Data stores 14: export agent run history to the data store" (outbox and export in `backend/src/aios/datastores/history.py`, store history line `datastores/migrations/versions/h0001_history.py` with branch label `history`, platform migration `0014`, tasks in `worker.py`, API in `api/settings.py`, outbox writes in `api/agents.py` `_end_turn`/deletes, Settings page `frontend/src/pages/Settings.tsx` + `lib/settings.ts`, tests `backend/tests/test_history_export.py` and `frontend/e2e/settings.spec.ts`). Read the issue with GitHub MCP tools (ToolSearch `select:mcp__github__issue_read`, owner EarthlingOllie, repo aios, issue 34) and the plan with `git show origin/claude/user-databases-plan:plan/user-databases/plan.md` (fetch that branch first) when you need context.

## Setup (in your worktree)
- `git fetch origin claude/data-stores-34-history-export && git checkout -b claude/ds34-f<N> origin/claude/data-stores-34-history-export` (N = your finding number). Commit there. Do NOT push and do NOT touch any other branch; the orchestrator cherry-picks from your local branch.
- Commit messages end with:
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01KC3fucdE9rANvvSzGLn7e5
- Your own isolated Postgres 16 cluster, so you don't collide with ~9 parallel agents: port P = 5800+N, cluster name `h<N>`:
  - `pg_createcluster 16 h<N> -p P --encoding UTF8 --locale C.UTF-8`
  - `sed -i 's/^local\s\+all\s\+postgres\s\+peer/local all postgres trust/' /etc/postgresql/16/h<N>/pg_hba.conf`
  - `pg_ctlcluster 16 h<N> start`
  - `psql -h /var/run/postgresql -p P -U postgres -c "CREATE ROLE aios LOGIN SUPERUSER PASSWORD 'aios'" -c 'CREATE DATABASE aios_h<N> OWNER aios'`
  - pgvector 0.8.1 is already installed for PG16.
- Your own Redis: `redis-server --port R --daemonize yes` with R = 6800+N. Add `--dir /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad` so dump.rdb stays out of the repo.
- Env (export before backend commands, from `backend/`):
  - DATABASE_ADMIN_URL=postgresql+psycopg://aios:aios@localhost:P/aios_h<N>
  - DATABASE_URL=postgresql+psycopg://aios_app:aios_app@localhost:P/aios_h<N>
  - DATA_STORE_PROVISIONER_URL=postgresql+psycopg://aios_provisioner:aios_provisioner@localhost:P/aios_h<N>
  - REDIS_URL=redis://localhost:R/0
  - Do not set SCHEDULER_ENABLED=false.
- Backend: `uv run python -m aios.db_roles && uv run alembic upgrade head && uv run python -m aios.datastores.provision migrate-all && uv run pytest -q -p no:cacheprovider`. Lint: `.venv/bin/ruff check . && .venv/bin/ruff format --check .`
- Frontend (only if your fix touches it or the UI behaviour it causes): in `frontend/`, `pnpm install --frozen-lockfile`, `pnpm typecheck && pnpm test`, and `E2E_PORT=<5900+N> PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium-1194/chrome-linux/chrome pnpm exec playwright test`. Never run `playwright install`.
- A real-app Playwright drive harness from the builder is at /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds34/drive/ (run.sh, serve.py, drive.cjs, store.py, reset.py: real API + provisioning worker with beat + default-queue worker; Settings toggle, backfill, new turn, delete). Copy it and change its ports/paths/env to yours if you use it; don't edit the original.
- When done: `pg_ctlcluster 16 h<N> stop && pg_dropcluster 16 h<N>` and `redis-cli -p R shutdown`.

## Process
1. Investigate: trace a realistic path from a real caller/input to the failure. Decide REAL or NOT REAL. Weigh the code a fix adds against what it prevents; if the path is far-fetched or the fix costs more than it buys, say so and build nothing.
2. If real: invoke the Skill tool with skill `mattpocock-skills:tdd`. Write the failing test first and save its red output, then the minimal fix, then save the green output. Keep the fix minimal and in the style of the surrounding code (match comment density, naming). Don't widen scope; don't touch unrelated findings.
3. Run the full backend suite (and frontend/Playwright if relevant) and save the outputs. If the behaviour is user-visible, drive it with Playwright and save screenshots/logs.
4. Save all evidence to /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds34/f<N>/.
5. Return: verdict (REAL/NOT REAL/PARTLY) with the traced path, what you changed (files, short rationale), commit sha(s) on `claude/ds34-f<N>`, test counts, and the absolute paths of the evidence files (red, green, full-suite, any Playwright).

## Extra context for #34
- DATABASE_ADMIN_URL MUST be set when you run the suite (otherwise ~20 tests silently skip). The builder's baseline was 347 passed, 1 skipped.
- Known flaky test: `test_sandbox.py::test_egress_proxy_refusals[CONNECT other.com…]`. Rerun it once if it fails.
- On a fresh database a single agent-turn test can hang on the checkpointer's `CREATE INDEX CONCURRENTLY`. Create its tables first (/tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds26/f1/scripts/setup_checkpointer.py), or run the full suite.
- A killed run can leave a test user and `ds_*` databases behind. Clean them up before trusting the next run.
- Several findings touch `history.py`. Keep your diff minimal and inside your finding's code path so cherry-picks merge.
- Usage limits have interrupted agents before. Commit work in progress often.
