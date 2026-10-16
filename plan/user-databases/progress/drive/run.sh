#!/bin/bash
# Usage: run.sh <label> [backend src dir to put first on PYTHONPATH]
# Starts the API (serve.py) on 8181 and vite on 5181, drives the app, then stops both.
set -u
D=/tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/drive
WT=/home/user/aios/.claude/worktrees/agent-adf32d3f8ad761786
OUT=$D/out-$1
mkdir -p "$OUT"
psql -h localhost -p 5442 -U aios -d aios_ds21 -c "DELETE FROM users WHERE github_id = -500" >/dev/null

export DATABASE_URL=postgresql+psycopg://aios:aios@localhost:5442/aios_ds21
export PUBLIC_URL=http://localhost:5181
export SCHEDULER_ENABLED=false
export PYTHONPATH=${2:-}
cd $WT/backend
uv run python $D/serve.py 8181 >"$OUT/api.log" 2>&1 &
API=$!
cd $WT/frontend
BACKEND_URL=http://localhost:8181 pnpm exec vite --port 5181 --strictPort >"$OUT/vite.log" 2>&1 &
VITE=$!
for _ in $(seq 60); do
  curl -sf http://localhost:8181/healthz >/dev/null && curl -sf http://localhost:5181 >/dev/null && break
  sleep 1
done
node $D/drive.mjs http://localhost:5181 "$OUT" >"$OUT/drive.log" 2>&1
echo "drive exit $?"
kill $API $VITE
wait 2>/dev/null
psql -h localhost -p 5442 -U aios -d aios_ds21 -Atc "SELECT count(*) FROM data_stores ds JOIN users u ON u.id = ds.owner_user_id WHERE u.github_id = -500" >"$OUT/store_rows.txt" 2>&1
