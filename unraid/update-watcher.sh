#!/bin/bash
# Host-side updater for Conserve Mass Ops. Lets the "Update now" button work
# without giving the container any access to Docker.
#
# Install once with the Unraid "User Scripts" plugin:
#   Settings -> User Scripts -> Add New Script -> name: conserve-mass-updater
#   Script content:   bash /mnt/user/appdata/conserve-mass-src/update-watcher.sh
#   Schedule:         Custom ->  * * * * *     (every minute)
set -uo pipefail

APPDATA=/mnt/user/appdata/conserve-mass
SRC=/mnt/user/appdata/conserve-mass-src
FLAG="$APPDATA/update-requested"
STATUS="$APPDATA/update-status"
LOG="$APPDATA/update-last.log"
LOCK=/tmp/conserve-mass-update.lock

WATCHER_VERSION=2   # 2: understands "tag=" (roll back to a specific build)

# Heartbeat: tells the app the watcher is installed and running, and what it supports.
echo "watcher=$WATCHER_VERSION" > "$APPDATA/updater-heartbeat" && chmod 644 "$APPDATA/updater-heartbeat"

[ -f "$FLAG" ] || exit 0

# Never run two updates at once.
exec 9>"$LOCK"
flock -n 9 || exit 0

# Optional exact image to switch to (rollback). Only strict tag shapes are accepted:
# the request file is written by the app, so never trust it further than that.
TAG=$(grep -E '^tag=' "$FLAG" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' \r')
if [ -n "$TAG" ] && ! [[ "$TAG" =~ ^(sha-[0-9a-f]{40}|[0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
    TAG=""
fi
rm -f "$FLAG"
started=$(date -Iseconds)
printf 'result=running\nstarted=%s\n' "$started" > "$STATUS"
chmod 644 "$STATUS"

if bash "$SRC/update.sh" ${TAG:+"$TAG"} > "$LOG" 2>&1; then result=ok; else result=failed; fi
chmod 644 "$LOG"

version=$(docker exec conserve-mass python -c \
  "from app.config import APP_VERSION; print(APP_VERSION)" 2>/dev/null || echo unknown)
printf 'result=%s\nstarted=%s\nfinished=%s\nversion=%s\n' \
  "$result" "$started" "$(date -Iseconds)" "$version" > "$STATUS"
chmod 644 "$STATUS"
