#!/bin/bash
# Starts the real provisioning worker, the real ingest worker (fakes for Docling/Voyage), the
# API (serve.py, fake GitHub and chat model) on 8531 and vite on 5531; drives the app; stops all.
set -u
D=/tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds2426
WT=/home/user/aios/.claude/worktrees/agent-ab17abbcec02c8f74
BE=$D/be.sh
OUT=$D/drive/out
PG=postgresql://aios:aios@localhost:5530
rm -rf "$OUT"; mkdir -p "$OUT"
export PUBLIC_URL=http://localhost:5531
export SCHEDULER_ENABLED=false
$BE uv run python $D/drive/reset.py
$BE uv run celery -A aios.worker worker -Q provisioning --loglevel=INFO --concurrency=1 \
  >"$OUT/provisioning-worker.log" 2>&1 &
PROV=$!
$BE uv run python $D/drive/ingest_worker.py >"$OUT/ingest-worker.log" 2>&1 &
INGEST=$!
$BE uv run python $D/drive/serve.py 8531 >"$OUT/api.log" 2>&1 &
API=$!
cd $WT/frontend
BACKEND_URL=http://localhost:8531 pnpm exec vite --port 5531 --strictPort >"$OUT/vite.log" 2>&1 &
VITE=$!
for _ in $(seq 60); do
  curl -sf http://localhost:8531/healthz >/dev/null && curl -sf http://localhost:5531 >/dev/null && break
  sleep 1
done
FRONTEND=$WT/frontend PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium-1194/chrome-linux/chrome \
  node $D/drive/drive.cjs http://localhost:5531 "$OUT" >"$OUT/drive.log" 2>&1
echo "drive exit $?"
kill $API $VITE $PROV $INGEST
pkill -f "celery -A aios.worker worker -Q provisioning" ; pkill -f "$D/drive/ingest_worker.py"
wait 2>/dev/null
psql "$PG/aios_ds2426" -c \
  "SELECT u.login, ds.status, ds.schema_revision, ds.dbname FROM data_stores ds
     JOIN users u ON u.id = ds.owner_user_id WHERE u.github_id IN (-2426, -2427)" \
  >"$OUT/stores.txt" 2>&1
for db in $(psql -At "$PG/aios_ds2426" -c \
    "SELECT ds.dbname FROM data_stores ds JOIN users u ON u.id = ds.owner_user_id
      WHERE u.github_id = -2426"); do
  psql "$PG/$db" -c "SELECT version_num FROM aios.alembic_version" -c "\dn" \
    -c "SELECT d.id, d.filename, d.status, count(c.id) AS chunks FROM aios.documents d
       LEFT JOIN aios.chunks c ON c.document_id = d.id GROUP BY d.id" >"$OUT/store_rows.txt" 2>&1
done
