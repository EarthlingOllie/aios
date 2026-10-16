set -e
. /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds37/env.sh
cd /home/user/aios/.claude/worktrees/agent-a5586cc5941c372f2/backend
uv sync -q
uv run python -m aios.db_roles
uv run alembic upgrade head
uv run python -m aios.datastores.provision migrate-all
uv run pytest -q -p no:cacheprovider 2>&1 | tail -15
.venv/bin/ruff check . && .venv/bin/ruff format --check .
uv run alembic check
