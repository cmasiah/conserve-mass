#!/bin/sh
# Unraid-friendly entrypoint: run as PUID:PGID (default 99:100 nobody:users),
# make sure /config is owned correctly, apply migrations, then start the app.
set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-022}"
CONFIG_DIR="${CONFIG_DIR:-/config}"

umask "$UMASK"

# Create group/user matching PUID/PGID if they don't already exist.
if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd -g "$PGID" cmass
fi
GROUP_NAME="$(getent group "$PGID" | cut -d: -f1)"

if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -d "$CONFIG_DIR" -s /usr/sbin/nologin cmass
fi
USER_NAME="$(getent passwd "$PUID" | cut -d: -f1)"

mkdir -p "$CONFIG_DIR/backups" "$CONFIG_DIR/logs"
# Only chown what we own; never recurse into anything unexpected.
chown "$PUID:$PGID" "$CONFIG_DIR" "$CONFIG_DIR/backups" "$CONFIG_DIR/logs"
find "$CONFIG_DIR" -maxdepth 2 \( -name '*.db*' -o -name '*.log' -o -name '*.sqlite3' \) \
    -exec chown "$PUID:$PGID" {} +

echo "[conserve-mass] running as ${USER_NAME}(${PUID}):${GROUP_NAME}(${PGID}) umask ${UMASK}"

cd /app
gosu "$PUID:$PGID" alembic upgrade head

exec gosu "$PUID:$PGID" "$@"
