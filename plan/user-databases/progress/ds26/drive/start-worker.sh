#!/bin/bash
# The real provisioning worker (the provisioning queue only), against the ds26 cluster.
D=/tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds26
cd /home/user/aios/.claude/worktrees/agent-a79ad339f55bbb311/backend || exit 1
export DATA_STORE_STATEMENT_TIMEOUT=3000
exec $D/env.sh uv run celery -A aios.worker worker -Q provisioning --loglevel=INFO --concurrency=1 \
  >"$D/drive/out/provisioning-worker.log" 2>&1
