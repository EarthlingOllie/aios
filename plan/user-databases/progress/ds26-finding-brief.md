# Brief for #26 review-finding agents

You investigate ONE code-review finding on branch `claude/data-stores-26-query` (head 6e129d0043d649cbcb2b6b07916a72ef6e4261ac) of EarthlingOllie/aios, which builds issue #26 "Data stores 6: agents can describe and query the data store" (new code in `backend/src/aios/tools/data.py`, tests in `backend/tests/test_data_tools.py`, store revision `datastores/migrations/versions/0002_data_schema.py`). Read the issue with GitHub MCP tools (ToolSearch `select:mcp__github__issue_read`, owner EarthlingOllie, repo aios, issue 26) and the plan with `git show origin/claude/user-databases-plan:plan/user-databases/plan.md` (fetch that branch first) when you need context.

## Setup (in your worktree)
- `git fetch origin claude/data-stores-26-query && git checkout -b claude/ds26-f<N> 6e129d0043d649cbcb2b6b07916a72ef6e4261ac` (N = your finding number). Commit there. Do NOT push and do NOT touch any other branch; the orchestrator cherry-picks from your local branch.
- Commit messages end with:
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01KC3fucdE9rANvvSzGLn7e5
- Your own isolated Postgres 16 cluster, so you don't collide with ~9 parallel agents: port P = 5470+N, cluster name `q<N>`:
  - `pg_createcluster 16 q<N> -p P --encoding UTF8 --locale C.UTF-8`
  - `sed -i 's/^local\s\+all\s\+postgres\s\+peer/local all postgres trust/' /etc/postgresql/16/q<N>/pg_hba.conf`
  - `pg_ctlcluster 16 q<N> start`
  - `psql -h /var/run/postgresql -p P -U postgres -c "CREATE ROLE aios LOGIN SUPERUSER PASSWORD 'aios'" -c 'CREATE DATABASE aios_q<N> OWNER aios'`
  - pgvector 0.8.1 is already installed for PG16.
- Your own Redis: `redis-server --port R --daemonize yes` with R = 6410+N. Add `--dir /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad` so dump.rdb stays out of the repo.
- Env (export before backend commands, from `backend/`):
  - DATABASE_ADMIN_URL=postgresql+psycopg://aios:aios@localhost:P/aios_q<N>
  - DATABASE_URL=postgresql+psycopg://aios_app:aios_app@localhost:P/aios_q<N>
  - DATA_STORE_PROVISIONER_URL=postgresql+psycopg://aios_provisioner:aios_provisioner@localhost:P/aios_q<N>
  - REDIS_URL=redis://localhost:R/0
  - Do not set SCHEDULER_ENABLED=false.
- Backend: `uv run python -m aios.db_roles && uv run alembic upgrade head && uv run python -m aios.datastores.provision migrate-all && uv run pytest -q -p no:cacheprovider`. Lint: `.venv/bin/ruff check . && .venv/bin/ruff format --check .`
- Frontend (only if your fix touches it or the UI behaviour it causes): in `frontend/`, `pnpm install --frozen-lockfile`, `pnpm typecheck && pnpm test`, and `E2E_PORT=<5210+N> PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium-1194/chrome-linux/chrome pnpm exec playwright test`. Never run `playwright install`.
- A real-app Playwright drive harness from the builder is at /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds26/drive/ (run.sh, serve.py, drive.cjs, setup.py, start-worker.sh: an agent with both data tools and a scripted fake model). Copy it and change its ports/paths/env to yours if you use it; don't edit the original.
- When done: `pg_ctlcluster 16 q<N> stop && pg_dropcluster 16 q<N>` and `redis-cli -p R shutdown`.

## Process
1. Investigate: trace a realistic path from a real caller/input to the failure. Decide REAL or NOT REAL. Weigh the code a fix adds against what it prevents; if the path is far-fetched or the fix costs more than it buys, say so and build nothing.
2. If real: invoke the Skill tool with skill `mattpocock-skills:tdd`. Write the failing test first and save its red output, then the minimal fix, then save the green output. Keep the fix minimal and in the style of the surrounding code (match comment density, naming). Don't widen scope; don't touch unrelated findings.
3. Run the full backend suite (and frontend/Playwright if relevant) and save the outputs. If the behaviour is user-visible, drive it with Playwright and save screenshots/logs.
4. Save all evidence to /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds26/f<N>/.
5. Return: verdict (REAL/NOT REAL/PARTLY) with the traced path, what you changed (files, short rationale), commit sha(s) on `claude/ds26-f<N>`, test counts, and the absolute paths of the evidence files (red, green, full-suite, any Playwright).

## Extra context for #26
- Branch #24 ("documents and chunks live in the user's data store") is being built in parallel from the same base and will rework `backend/src/aios/datastores/connections.py` (connection budgeting; possibly removing `documents_session`/`store_owner`) and search. Avoid editing `connections.py` and `search.py` unless your finding truly needs it; if it does, keep the edit minimal and say so explicitly in your report.
- Known flaky test: `test_sandbox.py::test_egress_proxy_refusals[CONNECT other.com…]`; rerun it once if it fails, and report it.
