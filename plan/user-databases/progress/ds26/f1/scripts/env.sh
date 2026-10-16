export DATABASE_ADMIN_URL=postgresql+psycopg://aios:aios@localhost:5471/aios_q1
export DATABASE_URL=postgresql+psycopg://aios_app:aios_app@localhost:5471/aios_q1
export DATA_STORE_PROVISIONER_URL=postgresql+psycopg://aios_provisioner:aios_provisioner@localhost:5471/aios_q1
export REDIS_URL=redis://localhost:6411/0
cd /home/user/aios/.claude/worktrees/agent-af0591f3a7dff022b/backend
exec "$@"
