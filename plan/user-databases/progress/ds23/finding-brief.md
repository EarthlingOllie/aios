# Brief for #23 review-finding agents

You investigate ONE code-review finding on branch `claude/data-stores-23-provision` (head 911df2c7a1297d6bfe7f50626858bcf4b0992c0e) of EarthlingOllie/aios, which builds issue #23 "Data stores 3: provision an isolated data store on sign-up". Read the issue with GitHub MCP tools (ToolSearch `select:mcp__github__issue_read`, owner EarthlingOllie, repo aios, issue 23) and the plan with `git show origin/claude/user-databases-plan:plan/user-databases/plan.md` (fetch that branch first) when you need context.

## Setup (in your worktree)
- `git fetch origin claude/data-stores-23-provision && git checkout -b claude/ds23-f<N> 911df2c7a1297d6bfe7f50626858bcf4b0992c0e` (N = your finding number). Commit there. Do NOT push and do NOT touch any other branch; the orchestrator cherry-picks from your local branch.
- Commit messages end with:
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01KC3fucdE9rANvvSzGLn7e5
- Your own isolated Postgres 16 cluster, so you don't collide with ~9 parallel agents: port P = 5450+N, cluster name `f<N>`:
  - `pg_createcluster 16 f<N> -p P --encoding UTF8 --locale C.UTF-8`
  - `sed -i 's/^local\s\+all\s\+postgres\s\+peer/local all postgres trust/' /etc/postgresql/16/f<N>/pg_hba.conf`
  - `pg_ctlcluster 16 f<N> start`
  - `psql -h /var/run/postgresql -p P -U postgres -c "CREATE ROLE aios LOGIN SUPERUSER PASSWORD 'aios'" -c 'CREATE DATABASE aios_f<N> OWNER aios'`
  - pgvector 0.8.1 is already installed for PG16.
- Your own Redis: `redis-server --port R --daemonize yes` with R = 6390+N.
- Env (export before backend commands, from `backend/`):
  - DATABASE_ADMIN_URL=postgresql+psycopg://aios:aios@localhost:P/aios_f<N>
  - DATABASE_URL=postgresql+psycopg://aios_app:aios_app@localhost:P/aios_f<N>
  - DATA_STORE_PROVISIONER_URL=postgresql+psycopg://aios_provisioner:aios_provisioner@localhost:P/aios_f<N>
  - REDIS_URL=redis://localhost:R/0
  - Do not set SCHEDULER_ENABLED=false.
- Backend: `uv run python -m aios.db_roles && uv run alembic upgrade head && uv run pytest -q -p no:cacheprovider`. Lint: `.venv/bin/ruff check . && .venv/bin/ruff format --check .`
- Frontend (only if your fix touches it or the UI behaviour it causes): in `frontend/`, `pnpm install --frozen-lockfile`, `pnpm typecheck && pnpm test`, and `E2E_PORT=<5190+N> PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium-1194/chrome-linux/chrome pnpm exec playwright test`. Never run `playwright install`.
- A real-app Playwright drive harness from the builder is at /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds23/drive/ (run.sh, serve.py, drive.cjs, start-worker.sh). Copy it and change its ports/paths/env to yours if you use it; don't edit the original.
- When done: `pg_ctlcluster 16 f<N> stop && pg_dropcluster 16 f<N>` and `redis-cli -p R shutdown`.

## Process
1. Investigate: trace a realistic path from a real caller/input to the failure. Decide REAL or NOT REAL. Weigh the code a fix adds against what it prevents; if the path is far-fetched or the fix costs more than it buys, say so and build nothing.
2. If real: invoke the Skill tool with skill `mattpocock-skills:tdd`. Write the failing test first and save its red output, then the minimal fix, then save the green output. Keep the fix minimal and in the style of the surrounding code (match comment density, naming). Don't widen scope; don't touch unrelated findings.
3. Run the full backend suite (and frontend/Playwright if relevant) and save the outputs. If the behaviour is user-visible, drive it with Playwright and save screenshots/logs.
4. Save all evidence to /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds23/f<N>/.
5. Return: verdict (REAL/NOT REAL/PARTLY) with the traced path, what you changed (files, short rationale), commit sha(s) on `claude/ds23-f<N>`, test counts, and the absolute paths of the evidence files (red, green, full-suite, any Playwright).
