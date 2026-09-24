#!/usr/bin/env bash
# Back up a running Community install before an upgrade or optional data purge.
set -euo pipefail
umask 077

INSTALL_DIR="${BOSS_INSTALL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
BACKUP_ROOT="${BOSS_BACKUP_DIR:-$HOME/boss-backups}"

usage() {
  cat <<'EOF'
Usage: backup.sh [--dir INSTALL_DIR] [--backup-dir BACKUP_ROOT]

Creates a private backup of .env, BOSS and optional Infisical Postgres databases,
and the BOSS artifacts volume. Writers pause during the snapshot and resume on
success or failure. Prints only the backup directory path on stdout.
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
  if command -v sudo >/dev/null 2>&1 && sudo -v && sudo docker info >/dev/null 2>&1; then
    DOCKER=(sudo docker)
  else
    echo 'Docker daemon is unavailable to this user.' >&2; exit 1
  fi
fi
COMPOSE=("${DOCKER[@]}" compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" --profile web --profile secrets)
"${COMPOSE[@]}" exec -T db pg_isready -U boss -d boss >/dev/null || {
  echo 'BOSS Postgres is not ready; backup cannot continue.' >&2; exit 1;
}

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

RUNNING="$("${COMPOSE[@]}" ps --status running --services)"
was_running() { [[ $'\n'"$RUNNING"$'\n' == *$'\n'"$1"$'\n'* ]]; }
PAUSED=()
for service in worker api web infisical; do
  if was_running "$service"; then PAUSED+=("$service"); fi
done

resume() {
  local service
  for service in infisical api worker web; do
    if [[ " ${PAUSED[*]} " == *" $service "* ]]; then
      "${COMPOSE[@]}" start "$service" >&2 || return 1
    fi
  done
}
on_exit() {
  local status=$?
  trap - EXIT
  if ! resume; then
    echo 'Backup could not restart every service; inspect docker compose ps.' >&2
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
  "${COMPOSE[@]}" stop "${PAUSED[@]}" >&2
fi
"${COMPOSE[@]}" exec -T db pg_dump -U boss -d boss -Fc > "$BACKUP/boss.dump"
if [[ "$("${COMPOSE[@]}" exec -T db psql -U boss -d boss -Atc "SELECT 1 FROM pg_database WHERE datname='infisical'")" == 1 ]]; then
  "${COMPOSE[@]}" exec -T db pg_dump -U boss -d infisical -Fc > "$BACKUP/infisical.dump"
fi
"${COMPOSE[@]}" run --rm --no-deps -T --entrypoint tar api \
  -C /var/lib/boss/artifacts -cf - . > "$BACKUP/artifacts.tar"
[[ -s "$BACKUP/boss.dump" && -s "$BACKUP/artifacts.tar" ]] || {
  echo 'Database or artifact archive was empty; refusing this backup.' >&2; exit 1;
}
printf 'source_commit=%s\ncreated_utc=%s\n' \
  "$(cat "$BACKUP/source-commit")" "$STAMP" > "$BACKUP/manifest"
resume
PAUSED=()
trap - EXIT
printf '%s\n' "$BACKUP"
