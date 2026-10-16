#!/bin/bash
# Run a command in the f3 worktree backend with this finding's env.
export DATABASE_ADMIN_URL=postgresql+psycopg://aios:aios@localhost:5803/aios_h3
export DATABASE_URL=postgresql+psycopg://aios_app:aios_app@localhost:5803/aios_h3
export DATA_STORE_PROVISIONER_URL=postgresql+psycopg://aios_provisioner:aios_provisioner@localhost:5803/aios_h3
export REDIS_URL=redis://localhost:6803/0
cd /home/user/aios/.claude/worktrees/agent-a7e70acb74e3b90fa/backend || exit 1
exec "$@"
