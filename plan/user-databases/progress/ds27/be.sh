#!/bin/bash
export DATABASE_ADMIN_URL=postgresql+psycopg://aios:aios@localhost:5527/aios_ds27
export DATABASE_URL=postgresql+psycopg://aios_app:aios_app@localhost:5527/aios_ds27
export DATA_STORE_PROVISIONER_URL=postgresql+psycopg://aios_provisioner:aios_provisioner@localhost:5527/aios_ds27
export REDIS_URL=redis://localhost:6527/0
cd /home/user/aios/.claude/worktrees/agent-ab560267bb39e730d/backend
exec "$@"
