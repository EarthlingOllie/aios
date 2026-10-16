#!/bin/bash
# Runs a command in the worktree's backend/ with this cluster's env.
export DATABASE_ADMIN_URL=postgresql+psycopg://aios:aios@localhost:5530/aios_ds2426
export DATABASE_URL=postgresql+psycopg://aios_app:aios_app@localhost:5530/aios_ds2426
export DATA_STORE_PROVISIONER_URL=postgresql+psycopg://aios_provisioner:aios_provisioner@localhost:5530/aios_ds2426
export REDIS_URL=redis://localhost:6530/0
cd /home/user/aios/.claude/worktrees/agent-ab17abbcec02c8f74/backend || exit 1
exec "$@"
