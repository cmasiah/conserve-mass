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

# Heartbeat: tells the app the watcher is installed and running.
touch "$APPDATA/updater-heartbeat" && chmod 644 "$APPDATA/updater-heartbeat"

[ -f "$FLAG" ] || exit 0

# Never run two updates at once.
exec 9>"$LOCK"
flock -n 9 || exit 0

rm -f "$FLAG"
started=$(date -Iseconds)
printf 'result=running\nstarted=%s\n' "$started" > "$STATUS"
chmod 644 "$STATUS"

if bash "$SRC/update.sh" > "$LOG" 2>&1; then result=ok; else result=failed; fi
chmod 644 "$LOG"

version=$(docker exec conserve-mass python -c \
  "from app.config import APP_VERSION; print(APP_VERSION)" 2>/dev/null || echo unknown)
printf 'result=%s\nstarted=%s\nfinished=%s\nversion=%s\n' \
  "$result" "$started" "$(date -Iseconds)" "$version" > "$STATUS"
chmod 644 "$STATUS"
