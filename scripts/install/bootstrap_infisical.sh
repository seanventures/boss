#!/usr/bin/env sh
# Provision the bundled Infisical and point BOSS at it, in one command.
#
# `infisical bootstrap` creates the admin, the organization and an instance machine identity,
# and hands back that identity's token. It does NOT create a project, and BOSS addresses every
# secret by project — so this also creates one and writes both values where BOSS reads them.
#
# THE TOKEN IS RETURNED ONCE. Re-running bootstrap on an instance that is already bootstrapped
# succeeds and returns nothing, so a script that discards it has locked itself out and the only
# way back is the web UI. That is why this writes it to the env file before doing anything else.
#
# Usage:  sh scripts/install/bootstrap_infisical.sh [ENV_FILE]
set -eu

ENV_FILE="${1:-.env}"
COMPOSE="${COMPOSE_FILE:-scripts/install/community-compose.yml}"
SERVICE="${INFISICAL_SERVICE:-infisical}"
ADMIN_EMAIL="${INFISICAL_ADMIN_EMAIL:-admin@boss.local}"
ORG_NAME="${INFISICAL_ORG:-boss}"
PROJECT_SLUG="${INFISICAL_PROJECT_SLUG:-boss-secrets}"   # Infisical requires 5+ characters
PORT="$(grep -s '^INFISICAL_PORT=' "$ENV_FILE" | cut -d= -f2)"
PORT="${PORT:-8080}"
URL="http://127.0.0.1:${PORT}"
# The address BOSS will use, which is NOT the one this script probes. From inside the api
# container 127.0.0.1 is that container, so a host URL written into .env configures a store
# the process cannot reach — and it fails as "not configured", pointing at the wrong thing.
# Bundled BOSS reaches it by service name on the compose network.
BOSS_URL="${BOSS_INFISICAL_URL:-http://infisical:8080}"

if grep -qs '^BOSS_INFISICAL_TOKEN=' "$ENV_FILE"; then
  echo "$ENV_FILE already holds BOSS_INFISICAL_TOKEN — nothing to do." >&2
  echo "Infisical hands the token back only at first bootstrap; keep the one you have." >&2
  exit 0
fi

# The one-line installer may use sudo Docker when a newly installed daemon is
# unavailable to the current user. Match that choice for bootstrap instead of
# failing the optional secret store while the rest of Compose works.
case "${BOSS_DOCKER_USE_SUDO:-auto}" in
  1) USE_SUDO_DOCKER=1 ;;
  0) USE_SUDO_DOCKER=0 ;;
  auto)
    if docker info >/dev/null 2>&1; then
      USE_SUDO_DOCKER=0
    elif command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
      USE_SUDO_DOCKER=1
    else
      echo "Docker daemon is unavailable to this user; use Docker access or noninteractive sudo." >&2
      exit 1
    fi ;;
  *) echo "BOSS_DOCKER_USE_SUDO must be 0 or 1." >&2; exit 1 ;;
esac
docker_compose() {
  if [ "$USE_SUDO_DOCKER" = 1 ]; then
    sudo -n docker compose "$@"
  else
    docker compose "$@"
  fi
}

# Generated if you did not supply one, and WRITTEN DOWN. The first version withheld it and
# said "reset it in the UI" — but a bundled instance has no SMTP, so password reset cannot
# send mail and the admin account was simply unreachable. Nobody could log in, ever.
#
# Keeping it out of the file protected nothing either: that same file holds the instance
# admin identity token, which grants more than the password does.
# Read from the ENV FILE as well as the environment. Taking it only from the environment
# meant a value the operator had written into the file they were told to configure was
# ignored, a different password was generated, and the file ended up holding two conflicting
# INFISICAL_ADMIN_PASSWORD lines — one that worked and one that did not, with nothing saying
# which. The file wins nothing here: it is simply consulted before generating.
ADMIN_PASSWORD="${INFISICAL_ADMIN_PASSWORD:-}"
if [ -z "$ADMIN_PASSWORD" ]; then
  ADMIN_PASSWORD="$(grep -s '^INFISICAL_ADMIN_PASSWORD=' "$ENV_FILE" | tail -1 | cut -d= -f2-)"
fi
ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(openssl rand -base64 24)}"

echo "waiting for Infisical at $URL ..." >&2
i=0
while [ "$i" -lt 60 ]; do
  if curl -fsS -m 5 "$URL/api/status" >/dev/null 2>&1; then break; fi
  i=$((i + 1)); sleep 2
done
if [ "$i" -ge 60 ]; then
  echo "Infisical did not answer at $URL. Is it up? Try:" >&2
  echo "  docker compose -f $COMPOSE --profile secrets ps" >&2
  exit 1
fi

echo "bootstrapping ..." >&2
# --env-file matters: the compose file REQUIRES INFISICAL_ENCRYPTION_KEY, so without it every
# compose subcommand — `exec` included — fails during interpolation before reaching the
# container. Its stderr is kept for the same reason: hiding it turned a missing flag into
# "this instance was already bootstrapped", which sent the operator to the web UI to fix a
# problem that was one argument away.
BOOT_ERR="$(mktemp)"
BOOT="$(docker_compose -f "$COMPOSE" --env-file "$ENV_FILE" exec -T "$SERVICE" \
  infisical bootstrap \
    --domain "http://127.0.0.1:8080" \
    --email "$ADMIN_EMAIL" \
    --password "$ADMIN_PASSWORD" \
    --organization "$ORG_NAME" \
    --output json --silent 2>"$BOOT_ERR" || true)"

TOKEN="$(printf '%s' "$BOOT" | tr -d '\n' | sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([A-Za-z0-9._-]*\)".*/\1/p')"
if [ -z "$TOKEN" ]; then
  echo "bootstrap returned no token. What it said:" >&2
  sed 's/^/  /' "$BOOT_ERR" >&2
  rm -f "$BOOT_ERR"
  echo "If the instance was already bootstrapped, the token cannot be re-issued — create a" >&2
  echo "machine identity in the UI at $URL and set BOSS_INFISICAL_TOKEN yourself." >&2
  exit 1
fi
rm -f "$BOOT_ERR"

# Write it down BEFORE creating the project. If project creation fails we can retry; if the
# token is lost there is no retry.
umask 077
{
  echo "BOSS_INFISICAL_URL=$BOSS_URL"
  echo "BOSS_INFISICAL_TOKEN=$TOKEN"
  grep -qs '^INFISICAL_ADMIN_EMAIL=' "$ENV_FILE" || echo "INFISICAL_ADMIN_EMAIL=$ADMIN_EMAIL"
  grep -qs '^INFISICAL_ADMIN_PASSWORD=' "$ENV_FILE" || echo "INFISICAL_ADMIN_PASSWORD=$ADMIN_PASSWORD"
} >> "$ENV_FILE"

PROJECT_ID="$(curl -fsS -m 30 -X POST "$URL/api/v2/workspace" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"projectName\":\"$PROJECT_SLUG\",\"slug\":\"$PROJECT_SLUG\"}" 2>/dev/null \
  | sed -n 's/.*"project"[[:space:]]*:[[:space:]]*{[[:space:]]*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"

if [ -z "$PROJECT_ID" ]; then
  echo "could not create the project. The token is saved in $ENV_FILE; create a project named" >&2
  echo "$PROJECT_SLUG at $URL and add BOSS_INFISICAL_PROJECT_ID yourself." >&2
  exit 1
fi

echo "BOSS_INFISICAL_PROJECT_ID=$PROJECT_ID" >> "$ENV_FILE"

echo "done. $ENV_FILE now points BOSS at Infisical."
echo "  url     $BOSS_URL   (BOSS reaches it here; you use $URL)"
echo "  project $PROJECT_ID"
echo "admin login at $URL"
echo "  email    $ADMIN_EMAIL"
echo "  password in $ENV_FILE as INFISICAL_ADMIN_PASSWORD"
echo "restart BOSS so it picks up the new settings."
