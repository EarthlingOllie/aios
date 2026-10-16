#!/bin/bash
# Starts the real provisioning worker, the real ingest worker (fakes for Docling/Voyage), the
# API (serve.py, fake GitHub and chat model) on 8184 and vite on 5184; drives the app; stops all.
set -u
D=/tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds24
WT=/home/user/aios/.claude/worktrees/agent-ae7258625986c1b9c
OUT=$D/drive/out
rm -rf "$OUT"; mkdir -p "$OUT"
export PUBLIC_URL=http://localhost:5184
export SCHEDULER_ENABLED=false
cd $WT/backend   # its .env points at the ds24 cluster and Redis
uv run python $D/drive/reset.py
uv run celery -A aios.worker worker -Q provisioning --loglevel=INFO --concurrency=1 \
  >"$OUT/provisioning-worker.log" 2>&1 &
PROV=$!
uv run python $D/drive/ingest_worker.py >"$OUT/ingest-worker.log" 2>&1 &
INGEST=$!
uv run python $D/drive/serve.py 8184 >"$OUT/api.log" 2>&1 &
API=$!
cd $WT/frontend
BACKEND_URL=http://localhost:8184 pnpm exec vite --port 5184 --strictPort >"$OUT/vite.log" 2>&1 &
VITE=$!
for _ in $(seq 60); do
  curl -sf http://localhost:8184/healthz >/dev/null && curl -sf http://localhost:5184 >/dev/null && break
  sleep 1
done
FRONTEND=$WT/frontend PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium-1194/chrome-linux/chrome \
  node $D/drive/drive.cjs http://localhost:5184 "$OUT" >"$OUT/drive.log" 2>&1
echo "drive exit $?"
kill $API $VITE $PROV $INGEST
wait 2>/dev/null
psql "postgresql://aios:aios@localhost:5444/aios_ds24" -c \
  "SELECT u.login, ds.status, ds.schema_revision, ds.dbname FROM data_stores ds
     JOIN users u ON u.id = ds.owner_user_id WHERE u.github_id IN (-2400, -2401)" \
  >"$OUT/stores.txt" 2>&1
psql "postgresql://aios:aios@localhost:5444/aios_ds24" -c "\dt public.*" >"$OUT/platform_tables.txt" 2>&1
for db in $(psql -At "postgresql://aios:aios@localhost:5444/aios_ds24" -c \
    "SELECT ds.dbname FROM data_stores ds JOIN users u ON u.id = ds.owner_user_id
      WHERE u.github_id IN (-2400, -2401) ORDER BY u.github_id DESC"); do
  echo "== $db" >>"$OUT/store_rows.txt"
  psql "postgresql://aios:aios@localhost:5444/$db" -c \
    "SELECT d.id, d.filename, d.status, count(c.id) AS chunks FROM aios.documents d
       LEFT JOIN aios.chunks c ON c.document_id = d.id GROUP BY d.id" >>"$OUT/store_rows.txt" 2>&1
done
