#!/bin/bash
# Starts the real ingest worker (fakes for Docling/Voyage), the API (serve.py, fake GitHub and
# chat model) on 8537 and vite on 5737, then drives the app. The provisioning worker starts only
# once the drive has seen the new user's store being set up (it writes out/start-provisioning).
set -u
D=/tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds37
WT=/home/user/aios/.claude/worktrees/agent-a5586cc5941c372f2
OUT=$D/drive/out
rm -rf "$OUT"; mkdir -p "$OUT"
. $D/env.sh
export PUBLIC_URL=http://localhost:5737
export SCHEDULER_ENABLED=false
cd $WT/backend
uv run python $D/drive/reset.py
uv run python $D/drive/ingest_worker.py >"$OUT/ingest-worker.log" 2>&1 &
INGEST=$!
uv run python $D/drive/serve.py 8537 >"$OUT/api.log" 2>&1 &
API=$!
(
  while [ ! -f "$OUT/start-provisioning" ]; do sleep 0.5; done
  exec uv run celery -A aios.worker worker -Q provisioning --loglevel=INFO --concurrency=1 \
    >"$OUT/provisioning-worker.log" 2>&1
) &
PROV=$!
cd $WT/frontend
BACKEND_URL=http://localhost:8537 pnpm exec vite --port 5737 --strictPort >"$OUT/vite.log" 2>&1 &
VITE=$!
for _ in $(seq 60); do
  curl -sf http://localhost:8537/healthz >/dev/null && curl -sf http://localhost:5737 >/dev/null && break
  sleep 1
done
FRONTEND=$WT/frontend PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium-1194/chrome-linux/chrome \
  node $D/drive/drive.cjs http://localhost:5737 "$OUT" >"$OUT/drive.log" 2>&1
echo "drive exit $?"
touch "$OUT/start-provisioning"
kill $API $VITE $INGEST
pkill -f "celery -A aios.worker worker -Q provisioning" 2>/dev/null
pkill -f "ds37/drive/ingest_worker.py" 2>/dev/null
pkill -f "ds37/drive/serve.py" 2>/dev/null
wait 2>/dev/null
for db in $(psql -At "postgresql://aios:aios@localhost:5537/aios_ds37" -c \
    "SELECT ds.dbname FROM data_stores ds JOIN users u ON u.id = ds.owner_user_id
      WHERE u.github_id = -3700"); do
  echo "== $db" >>"$OUT/store_rows.txt"
  psql "postgresql://aios:aios@localhost:5537/$db" -c \
    "SELECT d.id, d.filename, d.status, left(d.error, 60) AS error, count(c.id) AS chunks
       FROM aios.documents d LEFT JOIN aios.chunks c ON c.document_id = d.id GROUP BY d.id
       ORDER BY d.id" >>"$OUT/store_rows.txt" 2>&1
done
