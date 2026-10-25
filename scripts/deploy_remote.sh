#!/usr/bin/env bash
#
# Production deploy, run on the server.
#
#   bash scripts/deploy_remote.sh <image-ref> [deploy-path] [git-sha]
#
# The release workflow pipes this file over SSH so the newest version of the
# script is always the one executing:
#
#   ssh host bash -s -- <image-ref> <deploy-path> <git-sha> < scripts/deploy_remote.sh
#
# Called with no image reference it falls back to building locally, which is
# the manual-deploy path and keeps `deploy.sh` behaviour working.
#
# docker-compose.yml bind-mounts the checkout into /app, so the running Python
# code comes from the git working tree while the image supplies the installed
# dependencies. Both must move together: this script fast-forwards the checkout
# and switches the image tag before restarting anything.
#
# Migrations are applied before the switch and are NOT rolled back if the
# health probe fails. Write migrations to be backwards compatible with the
# previous release (add columns/tables first, drop them in a later deploy).
set -Eeuo pipefail

# The script itself arrives on stdin, so a credential prompt would eat the rest
# of it. Fail the fetch instead.
export GIT_TERMINAL_PROMPT=0

IMAGE_REF="${1:-}"
DEPLOY_PATH="${2:-$HOME/OrbitR}"
TARGET_SHA="${3:-}"
BACKUP_DIR="backups"

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

cd "$DEPLOY_PATH" || die "deploy path not found: $DEPLOY_PATH"
[ -f docker-compose.yml ] || die "docker-compose.yml missing in $DEPLOY_PATH"
[ -f .env ] || die ".env missing in $DEPLOY_PATH"
PREV_SHA="$(git rev-parse HEAD)"

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)

# Optional registry credentials for a private GHCR package. Kept in its own
# file so a token is never interpolated into a container's environment.
if [ -f .deploy.env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.deploy.env
  set +a
fi

HEALTH_URL="${APP_HEALTH_URL:-https://api.mwanachuoshop.com/health/}"
BACKUP_KEEP="${BACKUP_RETENTION:-5}"

read_env() { awk -F= -v key="$1" '$1 == key { print substr($0, index($0, "=") + 1); exit }' .env 2>/dev/null; }

PREV_IMAGE="$(read_env APP_IMAGE)"
PREV_PULL_POLICY="$(read_env APP_PULL_POLICY)"
ENV_TOUCHED=0

# An empty value is fine: compose falls back to the `${VAR:-default}` branch.
set_env() {
  local key="$1" value="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${value}|" .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
  ENV_TOUCHED=1
}

rollback() {
  local status=$?
  trap - ERR
  set +e
  if [ "$ENV_TOUCHED" = "1" ]; then
    echo "!! deploy failed (exit $status) - rolling back" >&2
    set_env APP_IMAGE "$PREV_IMAGE"
    set_env APP_PULL_POLICY "$PREV_PULL_POLICY"
    # /app is a bind mount, so the old image alone does not restore the old
    # code. The fast-forward merge only runs on a clean tree, and this guard
    # keeps that invariant before discarding anything.
    if [ -n "$PREV_SHA" ] && [ -z "$(git status --porcelain)" ]; then
      git reset --hard "$PREV_SHA"
    else
      echo "!! working tree is dirty - left code at $(git rev-parse --short HEAD); restore $PREV_SHA by hand" >&2
    fi
    "${COMPOSE[@]}" up -d
  fi
  exit "$status"
}
trap rollback ERR

log "Updating checkout in $DEPLOY_PATH"
git fetch --prune origin main
if [ -n "$TARGET_SHA" ]; then
  # --ff-only refuses to merge, so local edits on the server are never
  # silently discarded; they surface as a failed deploy instead.
  git merge --ff-only "$TARGET_SHA"
else
  git merge --ff-only origin/main
fi

if [ -z "$IMAGE_REF" ]; then
  log "No image reference given - building locally"
  set_env APP_IMAGE "orbitr-app:latest"
  set_env APP_PULL_POLICY "never"
  "${COMPOSE[@]}" build backend
else
  if [ -n "${GHCR_TOKEN:-}" ]; then
    [ -n "${GHCR_USER:-}" ] || die "GHCR_TOKEN is set but GHCR_USER is not"
    printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
  fi
  log "Pulling $IMAGE_REF"
  set_env APP_IMAGE "$IMAGE_REF"
  set_env APP_PULL_POLICY "always"
  "${COMPOSE[@]}" pull backend celery-worker celery-worker-payments celery-beat storage-init
fi

mkdir -p "$BACKUP_DIR"
if "${COMPOSE[@]}" ps --status running --services | grep -qx db; then
  stamp="$(date +%Y%m%d-%H%M%S)"
  backup="$BACKUP_DIR/db-$stamp.sql.gz"
  log "Backing up database to $backup"
  # stdin is redirected everywhere: this script itself is arriving on stdin.
  "${COMPOSE[@]}" exec -T db sh -c \
    'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' </dev/null | gzip > "$backup"
  # shellcheck disable=SC2012
  ls -1t "$BACKUP_DIR"/db-*.sql.gz | tail -n +$((BACKUP_KEEP + 1)) | xargs -r rm -f
else
  log "Database container not running - skipping backup (first deploy?)"
fi

log "Applying migrations"
"${COMPOSE[@]}" run -T --rm backend python manage.py migrate --noinput </dev/null

log "Collecting static files"
"${COMPOSE[@]}" run -T --rm backend python manage.py collectstatic --noinput </dev/null

log "Restarting services"
# No --remove-orphans: certbot is behind a compose profile and would be
# treated as an orphan and stopped, silently ending certificate renewal.
"${COMPOSE[@]}" up -d </dev/null

probe() {
  if command -v curl > /dev/null 2>&1; then
    curl -fsS --max-time 10 "$HEALTH_URL" > /dev/null
  elif command -v wget > /dev/null 2>&1; then
    wget -q -O /dev/null --timeout=10 "$HEALTH_URL"
  else
    "${COMPOSE[@]}" exec -T backend python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/', timeout=10)" </dev/null
  fi
}

log "Waiting for $HEALTH_URL"
for _ in $(seq 1 20); do
  if probe; then
    trap - ERR
    log "Deployment healthy: ${IMAGE_REF:-orbitr-app:latest} ${TARGET_SHA:-(HEAD)}"
    exit 0
  fi
  sleep 5
done

echo "health probe failed after 20 attempts" >&2
false
