#!/bin/bash
# Run a command in the backend dir against the ds23 test environment.
cd /home/user/aios/.claude/worktrees/agent-a5755f85feb67858d/backend || exit 1
. /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds23/env.sh
exec "$@"
