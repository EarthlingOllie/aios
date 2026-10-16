#!/bin/bash
# Usage: pt.sh <outfile> <pytest args...>  (runs backend pytest against cluster q2)
out=$1; shift
psql "postgresql://aios:aios@localhost:5472/aios_q2" -qc "DELETE FROM users WHERE github_id=-1" >/dev/null 2>&1
cd /home/user/aios/.claude/worktrees/agent-a0301984e5338048a/backend
export DATABASE_ADMIN_URL=postgresql+psycopg://aios:aios@localhost:5472/aios_q2
export DATABASE_URL=postgresql+psycopg://aios_app:aios_app@localhost:5472/aios_q2
export DATA_STORE_PROVISIONER_URL=postgresql+psycopg://aios_provisioner:aios_provisioner@localhost:5472/aios_q2
export REDIS_URL=redis://localhost:6412/0
timeout ${PT_TIMEOUT:-1500} uv run pytest -p no:cacheprovider "$@" > "$out" 2>&1
echo "exit=$?"
tail -5 "$out"
