#!/usr/bin/env bash
# Runs infra/deploy.sh (of the worktree given) against fake terraform/aws/docker, for a stack
# whose services are running and for a fresh one, and checks the order of its calls.
set -uo pipefail
H=/tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/deployharness
chmod +x "$H"/bin/*
status=0
for stack in running fresh; do
  echo "== $stack stack"
  export HARNESS_LOG="$H/$stack.log" HARNESS_STACK="$stack"
  : > "$HARNESS_LOG"
  PATH="$H/bin:$PATH" bash "$1/infra/deploy.sh" newtag > "$H/$stack.out" 2>&1 || { echo "deploy.sh failed:"; cat "$H/$stack.out"; }
  python3 "$H/check.py" "$stack" "$HARNESS_LOG" || status=1
done
exit $status
