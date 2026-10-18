#!/bin/sh
# Starts as root only to hand the volumes to the dev user, then drops to it.
set -e
for dir in /home/dev/.claude /home/dev/.venv /home/dev/.cache /workspace/frontend/node_modules; do
    mkdir -p "$dir"
    chown dev:dev "$dir"
done
exec setpriv --reuid=dev --regid=dev --init-groups env HOME=/home/dev "$@"
