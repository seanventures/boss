#!/usr/bin/env bash
# Back up a running or safely stopped Community install before an upgrade or data purge.
set -euo pipefail
umask 077

INSTALL_DIR="${BOSS_INSTALL_DIR:-}"
BACKUP_ROOT="${BOSS_BACKUP_DIR:-$HOME/boss-backups}"

usage() {
  cat <<'EOF'
Usage: backup.sh [--dir INSTALL_DIR] [--backup-dir BACKUP_ROOT]

Creates a private backup of .env, BOSS and optional Infisical Postgres databases,
and the BOSS artifacts volume. Writers pause during the snapshot and resume on
success or failure. If Postgres was stopped, it starts only for the snapshot
and is stopped again before this command returns. Prints only the backup
directory path on stdout.
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="${2:?--dir needs a path}"; shift 2 ;;
    --backup-dir) BACKUP_ROOT="${2:?--backup-dir needs a path}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done
if [[ -z "$INSTALL_DIR" ]]; then
  [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]] || {
    echo 'A piped backup needs --dir PATH or BOSS_INSTALL_DIR.' >&2; exit 2;
  }
  INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fi

INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd -P)"
ENV_FILE="$INSTALL_DIR/.env"
COMPOSE_FILE="$INSTALL_DIR/scripts/install/community-compose.yml"
[[ -f "$ENV_FILE" && -f "$COMPOSE_FILE" ]] || {
  echo 'BOSS checkout and private .env are required before backup.' >&2; exit 1;
}
[[ -d "$INSTALL_DIR/.git" || -f "$INSTALL_DIR/.git" || -f "$INSTALL_DIR/.boss-shipped-commit" ]] || {
  echo 'A Git checkout or remote.sh source-commit marker is required.' >&2; exit 1;
}
mkdir -p "$BACKUP_ROOT"
BACKUP_ROOT="$(cd "$BACKUP_ROOT" && pwd -P)"
case "$BACKUP_ROOT/" in
  "$INSTALL_DIR/"*) echo 'Backups must be outside the source checkout.' >&2; exit 1 ;;
esac

DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker)
  else
    echo 'Docker daemon is unavailable to this user.' >&2; exit 1
  fi
fi
COMPOSE=("${DOCKER[@]}" compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" --profile web --profile secrets)
RUNNING="$("${COMPOSE[@]}" ps --status running --services)"
was_running() { [[ $'\n'"$RUNNING"$'\n' == *$'\n'"$1"$'\n'* ]]; }
EXISTING="$("${COMPOSE[@]}" ps --all --services)"
was_created() { [[ $'\n'"$EXISTING"$'\n' == *$'\n'"$1"$'\n'* ]]; }
if ! was_running db && ! was_created db; then
  echo 'The installed Postgres container is missing. See scripts/install/README.md, "Recover an older safe uninstall", before retrying; no database image was pulled or substituted.' >&2
  exit 1
fi
# Compose run may build a service that has both `build:` and `image:` when its
# tagged image is missing, even with `--pull never`. Refuse that before pausing
# writers or starting a stopped database.
if ! API_IMAGE="$("${COMPOSE[@]}" config --format json | python3 -c 'import json, sys; print(json.load(sys.stdin)["services"]["api"]["image"])')" ||
   [[ -z "$API_IMAGE" ]]; then
  echo 'Could not resolve the installed API image for the artifact backup.' >&2
  exit 1
fi
if ! "${DOCKER[@]}" image inspect "$API_IMAGE" >/dev/null 2>&1; then
  echo "The installed API image $API_IMAGE is unavailable locally; backup will not build or pull it. Restore the reviewed local image before retrying." >&2
  exit 1
fi
if was_running db; then
  "${COMPOSE[@]}" exec -T db pg_isready -U boss -d boss </dev/null >/dev/null || {
    echo 'BOSS Postgres is running but not ready; backup cannot continue.' >&2; exit 1;
  }
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$BACKUP_ROOT/boss-$STAMP-$$"
mkdir -m 700 "$BACKUP"
cp -p "$ENV_FILE" "$BACKUP/.env"
chmod 600 "$BACKUP/.env"
if [[ -d "$INSTALL_DIR/.git" || -f "$INSTALL_DIR/.git" ]]; then
  git -C "$INSTALL_DIR" rev-parse HEAD > "$BACKUP/source-commit"
else
  cp "$INSTALL_DIR/.boss-shipped-commit" "$BACKUP/source-commit"
fi

PAUSED=()
for service in worker api web infisical; do
  if was_running "$service"; then PAUSED+=("$service"); fi
done
DB_STARTED_FOR_BACKUP=0

restore_services() {
  local service failed=0
  if [[ "$DB_STARTED_FOR_BACKUP" -eq 1 ]]; then
    "${COMPOSE[@]}" stop db </dev/null >&2 || failed=1
  fi
  for service in infisical api worker web; do
    if [[ " ${PAUSED[*]-} " == *" $service "* ]]; then
      "${COMPOSE[@]}" start "$service" </dev/null >&2 || failed=1
    fi
  done
  return "$failed"
}
on_exit() {
  local status=$?
  trap - EXIT
  if ! restore_services; then
    echo 'Backup could not restore the previous service state; inspect docker compose ps.' >&2
    status=1
  fi
  if [[ "$status" -ne 0 ]]; then
    echo "Backup incomplete; upgrade/purge must not continue. Inspect $BACKUP" >&2
  fi
  exit "$status"
}
trap on_exit EXIT

if [[ ${#PAUSED[@]} -gt 0 ]]; then
  echo 'Pausing BOSS writers for a consistent backup...' >&2
  "${COMPOSE[@]}" stop "${PAUSED[@]}" </dev/null >&2
fi
if ! was_running db; then
  echo 'Starting the existing BOSS database temporarily for a recovery backup...' >&2
  DB_STARTED_FOR_BACKUP=1
  # Compose start uses the stopped container's original image ID. `up` can
  # recreate it from a mutable image tag even when pulling is disabled.
  "${COMPOSE[@]}" start db </dev/null >&2
  ready=0
  for attempt in {1..60}; do
    if "${COMPOSE[@]}" exec -T db pg_isready -U boss -d boss </dev/null >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 2
  done
  [[ "$ready" -eq 1 ]] || { echo 'BOSS Postgres did not become ready; backup cannot continue.' >&2; exit 1; }
fi
"${COMPOSE[@]}" exec -T db pg_dump -U boss -d boss -Fc </dev/null > "$BACKUP/boss.dump"
if [[ "$("${COMPOSE[@]}" exec -T db psql -U boss -d boss -Atc "SELECT 1 FROM pg_database WHERE datname='infisical'" </dev/null)" == 1 ]]; then
  "${COMPOSE[@]}" exec -T db pg_dump -U boss -d infisical -Fc </dev/null > "$BACKUP/infisical.dump"
fi
"${COMPOSE[@]}" run --rm --no-deps --pull never -T --entrypoint tar api \
  -C /var/lib/boss/artifacts -cf - . </dev/null > "$BACKUP/artifacts.tar"
[[ -s "$BACKUP/boss.dump" && -s "$BACKUP/artifacts.tar" ]] || {
  echo 'Database or artifact archive was empty; refusing this backup.' >&2; exit 1;
}
printf 'source_commit=%s\ncreated_utc=%s\n' \
  "$(cat "$BACKUP/source-commit")" "$STAMP" > "$BACKUP/manifest"
restore_services
PAUSED=()
DB_STARTED_FOR_BACKUP=0
trap - EXIT
printf '%s\n' "$BACKUP"
