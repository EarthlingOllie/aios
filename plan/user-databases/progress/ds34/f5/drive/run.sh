#!/bin/bash
# Finding 5's drive: the API (serve.py) on 8205, vite on 5205, the provisioning worker (with its
# beat) and an app worker, on the h5 cluster and Redis 6805; drives delete-while-on; stops them.
set -u
D=/tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds34/f5
WT=/home/user/aios/.claude/worktrees/agent-a24cbea290d2f1cca
OUT=$D/drive/out
mkdir -p "$OUT"
export DATABASE_ADMIN_URL=postgresql+psycopg://aios:aios@localhost:5805/aios_h5
export DATABASE_URL=postgresql+psycopg://aios_app:aios_app@localhost:5805/aios_h5
export DATA_STORE_PROVISIONER_URL=postgresql+psycopg://aios_provisioner:aios_provisioner@localhost:5805/aios_h5
export REDIS_URL=redis://localhost:6805/0
export PUBLIC_URL=http://localhost:5205
export SCHEDULER_ENABLED=false
cd $WT/backend
uv run python $D/drive/reset.py
uv run python $D/drive/serve.py 8205 >"$OUT/api.log" 2>&1 &
API=$!
uv run celery -A aios.worker worker -Q provisioning --loglevel=INFO --concurrency=1 \
  -B --schedule=$D/drive/celerybeat-schedule -n prov5@%h >"$OUT/provisioning-worker.log" 2>&1 &
PROV=$!
uv run celery -A aios.worker worker --loglevel=INFO --concurrency=2 -n app5@%h \
  >"$OUT/app-worker.log" 2>&1 &
APPW=$!
cd $WT/frontend
BACKEND_URL=http://localhost:8205 pnpm exec vite --port 5205 --strictPort >"$OUT/vite.log" 2>&1 &
VITE=$!
for _ in $(seq 60); do
  curl -sf http://localhost:8205/healthz >/dev/null && curl -sf http://localhost:5205 >/dev/null && break
  sleep 1
done
FRONTEND=$WT/frontend BACKEND=$WT/backend STORE_PY=$D/drive/store.py \
  PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium-1194/chrome-linux/chrome \
  node $D/drive/drive.cjs http://localhost:5205 "$OUT" >"$OUT/drive.log" 2>&1
echo "drive exit $?"
kill $API $VITE $PROV $APPW
wait 2>/dev/null
