#!/bin/bash
# Pull the newest published image and restart. Config, .env and database are untouched.
# Usage (Unraid terminal):  bash /mnt/user/appdata/conserve-mass-src/update.sh
set -euo pipefail

cd "$(dirname "$0")"

# Keep this folder's compose file / scripts in sync with GitHub (no git needed).
RAW=https://raw.githubusercontent.com/cmasiah/conserve-mass/main
for f in docker-compose.yml update.sh; do
    if curl -fsSL "$RAW/$f" -o "$f.new"; then mv "$f.new" "$f"; else rm -f "$f.new"; fi
done

echo "==> Pulling latest image"
docker compose pull
echo "==> Restarting"
docker compose up -d
docker image prune -f >/dev/null

sleep 3
echo "==> Running version:"
docker exec conserve-mass python -c "from app.config import APP_VERSION; print(APP_VERSION)"
echo "==> Done. Logs: Logs tab in the app, or docker logs -f conserve-mass"
