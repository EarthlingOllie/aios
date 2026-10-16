#!/bin/bash
# Usage: run.sh <cmd...>   (runs in backend/ with finding-9 env)
export DATABASE_ADMIN_URL=postgresql+psycopg://aios:aios@localhost:5479/aios_q9
export DATABASE_URL=postgresql+psycopg://aios_app:aios_app@localhost:5479/aios_q9
export DATA_STORE_PROVISIONER_URL=postgresql+psycopg://aios_provisioner:aios_provisioner@localhost:5479/aios_q9
export REDIS_URL=redis://localhost:6419/0
cd /home/user/aios/.claude/worktrees/agent-ae6ec8e0fb087a345/backend || exit 1
exec "$@"
