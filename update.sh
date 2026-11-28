#!/bin/bash
# Pull the newest published image and restart. Config, .env and database are untouched.
# If the new version doesn't come up healthy within 3 minutes, roll back automatically.
# Usage (Unraid terminal):  bash /mnt/user/appdata/conserve-mass-src/update.sh
# Roll back to one build:   bash .../update.sh sha-<full commit id>   (or a version: 0.5.0)
#   That pins the image only for this run; the next plain update returns to the channel
#   set in .env.
set -euo pipefail

cd "$(dirname "$0")"
CONTAINER=conserve-mass

# Channel from the local .env: CMASS_IMAGE_TAG=latest (stable, main) or beta (beta branch).
TAG=$(grep -E '^[[:space:]]*CMASS_IMAGE_TAG=' .env 2>/dev/null | tail -1 | cut -d= -f2- \
      | tr -d "\"' \r" || true)
TAG=${TAG:-latest}
case "$TAG" in
    beta) BRANCH=beta ;;
    *)    BRANCH=main ;;
esac
echo "==> Channel: $TAG (scripts from the $BRANCH branch)"
PIN=${1:-}
if [ -n "$PIN" ]; then
    if ! [[ "$PIN" =~ ^(sha-[0-9a-f]{40}|[0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
        echo "!! Not an image tag this script will switch to: $PIN"; exit 2
    fi
    echo "==> Switching to the exact build $PIN (until the next normal update)"
    TAG=$PIN
    export CMASS_IMAGE_TAG=$PIN   # overrides .env for this run's docker compose calls
fi
IMAGE=ghcr.io/cmasiah/conserve-mass:$TAG

# Keep this folder's compose file / scripts in sync with GitHub (no git needed).
RAW=https://raw.githubusercontent.com/cmasiah/conserve-mass/$BRANCH
for pair in docker-compose.yml:docker-compose.yml update.sh:update.sh \
            unraid/update-watcher.sh:update-watcher.sh \
            deploy.env.example:deploy.env.example \
            docker-compose.override.example.yml:docker-compose.override.example.yml; do
    remote=${pair%%:*}; dest=${pair##*:}
    if curl -fsSL "$RAW/$remote" -o "$dest.new"; then mv "$dest.new" "$dest"; else rm -f "$dest.new"; fi
done

# Local, never-committed settings (see deploy.env.example / docker-compose.override.example.yml)
[ -f .env ] || echo "!! No local .env in $(pwd): using defaults (port 8000, TZ UTC). See deploy.env.example."
[ -f docker-compose.override.yml ] || echo "!! No docker-compose.override.yml: not joined to a proxy network. See docker-compose.override.example.yml."

old_id=$(docker inspect --format '{{.Image}}' "$CONTAINER" 2>/dev/null || true)

echo "==> Pulling $IMAGE"
docker compose pull
new_id=$(docker image inspect --format '{{.Id}}' "$IMAGE")

if [ -n "$old_id" ] && [ "$old_id" = "$new_id" ] \
   && docker exec "$CONTAINER" curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    echo "==> Already running the newest image."
    exit 0
fi

echo "==> Restarting"
docker compose up -d

echo "==> Waiting for the app to report healthy (up to 3 minutes)"
healthy=0
for _ in $(seq 1 36); do
    if docker exec "$CONTAINER" curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
        healthy=1; break
    fi
    sleep 5
done

if [ "$healthy" = 1 ]; then
    docker image prune -f >/dev/null
    echo "==> Running version:"
    docker exec "$CONTAINER" sh -c 'python -c "from app.config import APP_VERSION; print(APP_VERSION)"; echo "channel=$APP_CHANNEL commit=$APP_GIT_SHA"'
    echo "==> Done. Logs: Logs tab in the app, or docker logs -f $CONTAINER"
    exit 0
fi

echo "!! The new version did not start. Last log lines:"
docker logs --tail 40 "$CONTAINER" 2>&1 || true
if [ -n "$old_id" ] && [ "$old_id" != "$new_id" ]; then
    echo "==> Rolling back to the previous image"
    docker tag "$old_id" "$IMAGE"
    docker compose up -d
    echo "==> Rolled back. The broken version stays skipped until a fixed one is published."
fi
exit 1
