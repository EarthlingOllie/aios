#!/bin/bash
# Starts the API (serve.py) on 8186 and vite on 5196, drives the app, then stops both.
set -u
D=/tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds26
WT=/home/user/aios/.claude/worktrees/agent-a79ad339f55bbb311
OUT=$D/drive/out
rm -rf "$OUT"
mkdir -p "$OUT"
export PUBLIC_URL=http://localhost:5196
export SCHEDULER_ENABLED=false
export DATA_STORE_STATEMENT_TIMEOUT=3000
cd $WT/backend
$D/env.sh uv run python $D/drive/reset.py
$D/env.sh uv run python $D/drive/serve.py 8186 >"$OUT/api.log" 2>&1 &
API=$!
cd $WT/frontend
BACKEND_URL=http://localhost:8186 pnpm exec vite --port 5196 --strictPort >"$OUT/vite.log" 2>&1 &
VITE=$!
for _ in $(seq 60); do
  curl -sf http://localhost:8186/healthz >/dev/null && curl -sf http://localhost:5196 >/dev/null && break
  sleep 1
done
FRONTEND=$WT/frontend BACKEND=$WT/backend ENVSH=$D/env.sh \
  PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium-1194/chrome-linux/chrome \
  node $D/drive/drive.cjs http://localhost:5196 "$OUT" $D/drive/start-worker.sh $D/drive/setup.py \
  >"$OUT/drive.log" 2>&1
echo "drive exit $?"
kill $API $VITE
pkill -f "celery -A aios.worker worker -Q provisioning" || true
wait 2>/dev/null
