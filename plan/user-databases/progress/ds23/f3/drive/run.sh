#!/bin/bash
# Usage: run.sh <label> <backend dir> [PYTHONPATH]. API on 8193, vite on 5193.
set -u
F=/tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds23/f3
LABEL=$1; BE=$2; export PYTHONPATH=${3:-}
FE=/home/user/aios/.claude/worktrees/ds23-f3/frontend
OUT=$F/drive/out; mkdir -p "$OUT"
. $F/env.sh
export PUBLIC_URL=http://localhost:5193
cd $BE
.venv/bin/python $F/drive/setup.py 2>&1 | grep -v Retry | tee "$OUT/$LABEL-setup.txt"
.venv/bin/python $F/drive/serve.py 8193 >"$OUT/$LABEL-api.log" 2>&1 &
API=$!
cd $FE
BACKEND_URL=http://localhost:8193 pnpm exec vite --port 5193 --strictPort >"$OUT/$LABEL-vite.log" 2>&1 &
VITE=$!
for _ in $(seq 60); do
  curl -sf http://localhost:8193/healthz >/dev/null && curl -sf http://localhost:5193 >/dev/null && break
  sleep 1
done
FRONTEND=$FE PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium-1194/chrome-linux/chrome \
  node $F/drive/drive.cjs http://localhost:5193 "$OUT" "$LABEL" 2>&1 | tee "$OUT/$LABEL-drive.txt"
kill $API $VITE; wait 2>/dev/null
