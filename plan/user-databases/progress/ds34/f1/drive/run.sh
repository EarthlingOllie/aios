#!/bin/bash
# Real app for finding 1: API 8101, vite 5101, provisioning worker (+beat), app worker; then drive.
# Usage: run.sh <out dir>
set -u
F=/tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds34/f1
WT=/home/user/aios/.claude/worktrees/agent-ad9f7ec867ab4b8b2
OUT=$1
mkdir -p "$OUT"
export DATABASE_ADMIN_URL=postgresql+psycopg://aios:aios@localhost:5801/aios_h1
export DATABASE_URL=postgresql+psycopg://aios_app:aios_app@localhost:5801/aios_h1
export DATA_STORE_PROVISIONER_URL=postgresql+psycopg://aios_provisioner:aios_provisioner@localhost:5801/aios_h1
export REDIS_URL=redis://localhost:6801/0
export PUBLIC_URL=http://localhost:5101
export SCHEDULER_ENABLED=false
cd $WT/backend
uv run python $F/drive/reset.py
uv run python $F/drive/serve.py 8101 >"$OUT/api.log" 2>&1 &
API=$!
rm -f $F/drive/celerybeat-schedule*
uv run celery -A aios.worker worker -Q provisioning --loglevel=INFO --concurrency=1 \
  -B --schedule=$F/drive/celerybeat-schedule -n provf1@%h >"$OUT/provisioning-worker.log" 2>&1 &
PROV=$!
uv run celery -A aios.worker worker --loglevel=INFO --concurrency=2 -n appf1@%h \
  >"$OUT/app-worker.log" 2>&1 &
APPW=$!
cd $WT/frontend
BACKEND_URL=http://localhost:8101 pnpm exec vite --port 5101 --strictPort >"$OUT/vite.log" 2>&1 &
VITE=$!
for _ in $(seq 60); do
  curl -sf http://localhost:8101/healthz >/dev/null && curl -sf http://localhost:5101 >/dev/null && break
  sleep 1
done
FRONTEND=$WT/frontend PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium-1194/chrome-linux/chrome \
  node $F/drive/drive.cjs http://localhost:5101 "$OUT" "${MODE:-new}" >"$OUT/drive.log" 2>&1
echo "drive exit $?"
kill $API $VITE $PROV $APPW
wait 2>/dev/null
