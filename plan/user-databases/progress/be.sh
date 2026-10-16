#!/bin/bash
# Run a command in the backend dir against the ds21 test database.
cd /home/user/aios/.claude/worktrees/agent-adf32d3f8ad761786/backend || exit 1
export DATABASE_URL=postgresql+psycopg://aios:aios@localhost:5442/aios_ds21
"$@"
