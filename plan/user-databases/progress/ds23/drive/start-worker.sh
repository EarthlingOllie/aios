#!/bin/bash
# The real provisioning worker (the provisioning queue only), against the ds23 cluster.
D=/tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds23
cd /home/user/aios/.claude/worktrees/agent-a5755f85feb67858d/backend || exit 1
. $D/env.sh
exec uv run celery -A aios.worker worker -Q provisioning --loglevel=INFO --concurrency=1 \
  >"$D/drive/out/provisioning-worker.log" 2>&1
