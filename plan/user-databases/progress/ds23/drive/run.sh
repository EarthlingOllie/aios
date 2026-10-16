#!/bin/bash
# Starts the API (serve.py) on 8183 and vite on 5183, drives the app, then stops both.
set -u
D=/tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds23
WT=/home/user/aios/.claude/worktrees/agent-a5755f85feb67858d
OUT=$D/drive/out
mkdir -p "$OUT"
. $D/env.sh
export PUBLIC_URL=http://localhost:5183
export SCHEDULER_ENABLED=false
# A fresh user each run (their store's database, if any, is dropped first).
cd $WT/backend
uv run python $D/drive/reset.py
uv run python $D/drive/serve.py 8183 >"$OUT/api.log" 2>&1 &
API=$!
cd $WT/frontend
BACKEND_URL=http://localhost:8183 pnpm exec vite --port 5183 --strictPort >"$OUT/vite.log" 2>&1 &
VITE=$!
for _ in $(seq 60); do
  curl -sf http://localhost:8183/healthz >/dev/null && curl -sf http://localhost:5183 >/dev/null && break
  sleep 1
done
FRONTEND=$WT/frontend PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium-1194/chrome-linux/chrome \
  node $D/drive/drive.cjs http://localhost:5183 "$OUT" $D/drive/start-worker.sh >"$OUT/drive.log" 2>&1
echo "drive exit $?"
kill $API $VITE
pkill -f "celery -A aios.worker worker -Q provisioning" || true
wait 2>/dev/null
psql "postgresql://aios:aios@localhost:5443/aios_ds23" -c \
  "SELECT ds.id, ds.status, ds.schema_revision, ds.dbname, ds.host, ds.port, ds.error,
          left(ds.rw_password, 12) || '...' AS rw_password_ciphertext
     FROM data_stores ds JOIN users u ON u.id = ds.owner_user_id WHERE u.github_id = -2300" \
  >"$OUT/store_row.txt" 2>&1
