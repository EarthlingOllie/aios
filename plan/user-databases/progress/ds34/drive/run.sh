#!/bin/bash
# Starts the API (serve.py) on 8134, vite on 5134, the provisioning worker (with its beat) and an
# app worker (default queue), drives the app, then stops them all.
set -u
D=/tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds34
WT=/home/user/aios/.claude/worktrees/agent-a176008e83aa78497
OUT=$D/drive/out
mkdir -p "$OUT"
. $D/env.sh
export PUBLIC_URL=http://localhost:5134
export SCHEDULER_ENABLED=false
cd $WT/backend
uv run python $D/drive/reset.py
uv run python $D/drive/serve.py 8134 >"$OUT/api.log" 2>&1 &
API=$!
uv run celery -A aios.worker worker -Q provisioning --loglevel=INFO --concurrency=1 \
  -B --schedule=$D/drive/celerybeat-schedule -n prov34@%h >"$OUT/provisioning-worker.log" 2>&1 &
PROV=$!
uv run celery -A aios.worker worker --loglevel=INFO --concurrency=2 -n app34@%h \
  >"$OUT/app-worker.log" 2>&1 &
APPW=$!
cd $WT/frontend
BACKEND_URL=http://localhost:8134 pnpm exec vite --port 5134 --strictPort >"$OUT/vite.log" 2>&1 &
VITE=$!
for _ in $(seq 60); do
  curl -sf http://localhost:8134/healthz >/dev/null && curl -sf http://localhost:5134 >/dev/null && break
  sleep 1
done
FRONTEND=$WT/frontend BACKEND=$WT/backend STORE_PY=$D/drive/store.py \
  PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium-1194/chrome-linux/chrome \
  node $D/drive/drive.cjs http://localhost:5134 "$OUT" >"$OUT/drive.log" 2>&1
echo "drive exit $?"
kill $API $VITE $PROV $APPW
wait 2>/dev/null
