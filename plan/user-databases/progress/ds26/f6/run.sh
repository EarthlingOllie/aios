# usage: run.sh OUTFILE [pytest args...]
. /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds26/f6/env.sh
out=$1; shift
timeout 590 .venv/bin/python -m pytest -q -p no:cacheprovider "$@" > "$out" 2>&1
echo rc=$?
grep -E "^E  |passed|failed" "$out" | tail -8
