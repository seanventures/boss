#!/usr/bin/env bash
# Back up a running or safely stopped Community install before an upgrade or data purge.
set -euo pipefail
umask 077
trap 'exit 130' INT
trap 'exit 143' TERM

INSTALL_DIR="${BOSS_INSTALL_DIR:-}"
BACKUP_ROOT="${BOSS_BACKUP_DIR:-$HOME/boss-backups}"
LEAVE_STOPPED=0
SESSION_KEYS_FILE=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ARCHIVE_HELPER="$SCRIPT_DIR/backup_archive.py"

usage() {
  cat <<'EOF'
Usage: backup.sh [--dir INSTALL_DIR] [--backup-dir BACKUP_ROOT]

Creates a private backup of .env, BOSS and optional Infisical Postgres databases,
the BOSS artifacts and identity-key volumes, and the local repositories volume (the bare git
repositories of local projects, their only copy until published) with a completion manifest. CLI session secrets (Claude setup token and Codex
auth JSON) in .env are left blank; their decoded values are refused in decompressed
database text and artifacts. Durable encrypted connection records are retained. Writers pause during the snapshot and resume on
success or failure. If Postgres was stopped, it starts only for the snapshot
and is stopped again before this command returns. Prints only the backup
directory path on stdout.
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="${2:?--dir needs a path}"; shift 2 ;;
    --leave-stopped) LEAVE_STOPPED=1; shift ;;
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
# ---- scoped sudo Docker fallback ------------------------------------------
# Keep this small boundary identical in the installer and lifecycle helpers.
# sudo resets selection variables and uses root's Docker configuration.
boss_sudo_scope_safe() {
  _boss_docker_os="$(uname -s)" || return 1
  [ "$_boss_docker_os" = Linux ] || return 1
  [ -z "${DOCKER_HOST:-}${DOCKER_CONTEXT:-}${DOCKER_CONFIG:-}${DOCKER_TLS:-}${DOCKER_TLS_VERIFY:-}${DOCKER_CERT_PATH:-}" ] || return 1
  if [ "${2:-}" != restore ]; then
  [ "${COMPOSE_PROJECT_NAME:-boss-community}" = boss-community ] || return 1
  [ "${BOSS_COMMUNITY_IMAGE_TAG:-boss-community}" = boss-community ] || return 1
  if [ -f "$1" ]; then
    [ -r "$1" ] || return 1
    awk '
      NR == 1 { sub(/^\357\273\277/, "") }
      /^[[:space:]]*(export[[:space:]]+)?(COMPOSE_PROJECT_NAME|BOSS_COMMUNITY_IMAGE_TAG)([[:space:]]|=|:|$)/ {
        if ($0 !~ /^(COMPOSE_PROJECT_NAME|BOSS_COMMUNITY_IMAGE_TAG)=(boss-community)?$/) exit 1
        split($0, field, "="); if (seen[field[1]]++) exit 1
      }
    ' "$1" || return 1
  fi
  fi
  _boss_docker_context="$(docker context show 2>/dev/null)" || return 1
  [ "$_boss_docker_context" = default ] || return 1
  _boss_docker_endpoint="$(docker context inspect default --format '{{.Endpoints.docker.Host}}' 2>/dev/null)" || return 1
  [ "$_boss_docker_endpoint" = unix:///var/run/docker.sock ]
}
# ---- end scoped sudo Docker fallback --------------------------------------

if ! docker info >/dev/null 2>&1; then
  boss_sudo_scope_safe "$ENV_FILE" || { echo "Cannot safely use sudo for this Docker selection. Use direct Docker access for custom projects, contexts or connection settings." >&2; exit 1; }
  if command -v sudo >/dev/null 2>&1 && sudo -n docker --host unix:///var/run/docker.sock info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker --host unix:///var/run/docker.sock)
  else
    echo 'Docker daemon is unavailable to this user.' >&2; exit 1
  fi
fi
# The images are tagged with the compose project name (install.sh), not "local".
export BOSS_COMMUNITY_IMAGE_TAG="${BOSS_COMMUNITY_IMAGE_TAG:-${COMPOSE_PROJECT_NAME:-boss-community}}"
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
LAYOUT="$("${COMPOSE[@]}" config --format json | python3 "$ARCHIVE_HELPER" layout)" || exit 1
API_IMAGE="$(printf '%s' "$LAYOUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_image"])')"
if ! API_IMAGE_ID="$("${DOCKER[@]}" image inspect --format '{{.Id}}' "$API_IMAGE" 2>/dev/null)"; then
  echo "The installed API image $API_IMAGE is unavailable locally; backup will not build or pull it. Restore the reviewed local image before retrying." >&2
  exit 1
fi
API_CONTAINER="$("${COMPOSE[@]}" ps --all -q api)"
if [[ -n "$API_CONTAINER" ]]; then
  INSTALLED_API_IMAGE_ID="$("${DOCKER[@]}" inspect --format '{{.Image}}' "$API_CONTAINER")"
  [[ "$API_IMAGE_ID" == "$INSTALLED_API_IMAGE_ID" ]] || {
    echo 'The API image tag differs from the installed container. Restore its reviewed local tag before backup; no image was pulled or substituted.' >&2
    exit 1
  }
fi
# Refuse missing volumes before Compose run could silently create an empty replacement.
for field in db_volume identity_volume artifacts_volume; do
  volume="$(printf '%s' "$LAYOUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$field")"
  "${DOCKER[@]}" volume inspect "$volume" >/dev/null || { echo 'A recovery volume is missing; original data must be preserved.' >&2; exit 1; }
done
# LOCAL-FIRST PROJECTS (BOSSAPP-915): the repos volume holds local projects' bare repositories.
# An install from before local-first declares none; one that declares it but never started with
# it has no repositories yet. Either way there is nothing to lose, and the archive says so.
REPOS_VOLUME="$(printf '%s' "$LAYOUT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("repos_volume") or "")')"
if [[ -n "$REPOS_VOLUME" ]] && ! "${DOCKER[@]}" volume inspect "$REPOS_VOLUME" >/dev/null 2>&1; then
  echo 'The local repositories volume has not been created yet; no local repositories to back up.' >&2
  REPOS_VOLUME=""
  LAYOUT="$(printf '%s' "$LAYOUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); d.pop("repos_volume",None); print(json.dumps(d))')"
fi
DB_CONTAINER="$("${COMPOSE[@]}" ps --all -q db)"
DB_IMAGE_ID="$("${DOCKER[@]}" inspect --format '{{.Image}}' "$DB_CONTAINER")"
if was_running db; then
  "${COMPOSE[@]}" exec -T db pg_isready -U boss -d boss </dev/null >/dev/null || {
    echo 'BOSS Postgres is running but not ready; backup cannot continue.' >&2; exit 1;
  }
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$BACKUP_ROOT/boss-$STAMP-$$"
mkdir -m 700 "$BACKUP"
trap 'rm -rf "$BACKUP"' EXIT
# Temporary inspection material stays inside the private incomplete archive.
SESSION_KEYS_FILE="$BACKUP/.excluded-sessions.json"
python3 "$ARCHIVE_HELPER" env "$ENV_FILE" "$BACKUP/.env" "$SESSION_KEYS_FILE"
printf '%s\n' "$LAYOUT" > "$BACKUP/.metadata.json"
python3 - "$BACKUP/.metadata.json" "$API_IMAGE_ID" "$DB_IMAGE_ID" "$STAMP" <<'PYMETA'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
metadata = json.loads(path.read_text())
metadata.update(api_image_id=sys.argv[2], db_image_id=sys.argv[3], created_utc=sys.argv[4])
path.write_text(json.dumps(metadata))
PYMETA
if [[ -d "$INSTALL_DIR/.git" || -f "$INSTALL_DIR/.git" ]]; then
  git -C "$INSTALL_DIR" rev-parse HEAD > "$BACKUP/source-commit"
else
  # An archive-only install (remote.sh: `git archive` ships no .git). remote.sh now writes
  # the full 40-hex commit here, but an older install's marker — or one made before that fix
  # — can hold a SHORT sha, or even a TAG's own object hash instead of the commit it points
  # at (`git rev-parse --short <tag>` names an annotated tag, not its commit, without
  # `^{commit}`). Resolved when there happens to be a repo here to resolve it against;
  # otherwise this says plainly what is wrong and how to fix it, rather than copying an
  # ambiguous value into the backup's own record of what it holds.
  marker="$(cat "$INSTALL_DIR/.boss-shipped-commit" 2>/dev/null || true)"
  if [[ "$marker" =~ ^[0-9a-f]{40}$ ]]; then
    printf '%s\n' "$marker" > "$BACKUP/source-commit"
  elif resolved="$(git -C "$INSTALL_DIR" rev-parse --verify "${marker}^{commit}" 2>/dev/null)" \
       && [[ "$resolved" =~ ^[0-9a-f]{40}$ ]]; then
    printf '%s\n' "$resolved" > "$BACKUP/source-commit"
  else
    {
      echo "$INSTALL_DIR/.boss-shipped-commit holds '${marker:-nothing}', not a full commit"
      echo "sha, and there is no Git checkout here to resolve it against. Run"
      echo "remote.sh update (or ship) once more from a Git checkout of the source so a"
      echo "full 40-character commit is recorded, then back up again."
    } >&2
    exit 1
  fi
fi

PAUSED=()
while IFS= read -r service; do
  [[ -z "$service" || "$service" == db ]] || PAUSED+=("$service")
done <<< "$RUNNING"
DB_STARTED_FOR_BACKUP=0

restore_services() {
  local failed=0
  if [[ "$DB_STARTED_FOR_BACKUP" -eq 1 ]]; then
    "${COMPOSE[@]}" stop db </dev/null >&2 || failed=1
  fi
  if [[ ${#PAUSED[@]} -gt 0 ]]; then
    "${COMPOSE[@]}" start "${PAUSED[@]}" </dev/null >&2 || failed=1
  fi
  return "$failed"
}

on_exit() {
  local status=$?
  trap - EXIT
  rm -f "$SESSION_KEYS_FILE" "$BACKUP/.metadata.json" "$BACKUP/.identity-state.json"
  # An interrupted or failed snapshot never has a completion claim.
  if [[ "$status" -ne 0 ]]; then rm -f "$BACKUP/manifest.json"; fi
  if ! restore_services; then
    echo 'Backup could not restore the previous service state; inspect docker compose ps.' >&2
    status=1
  fi
  if [[ "$status" -ne 0 ]]; then
    rm -rf "$BACKUP"
    echo "Backup incomplete and discarded; upgrade/purge must not continue." >&2
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
INFISICAL_EXISTS="$("${COMPOSE[@]}" exec -T db psql -v ON_ERROR_STOP=1 -U boss -d boss -Atc "SELECT 1 FROM pg_database WHERE datname='infisical'" </dev/null)"
if [[ "$INFISICAL_EXISTS" == 1 ]]; then
  "${COMPOSE[@]}" exec -T db pg_dump -U boss -d infisical -Fc </dev/null > "$BACKUP/infisical.dump"
fi
# Read the snapshot's non-secret key check value while all application writers are stopped.
IDENTITY_TABLES="$("${COMPOSE[@]}" exec -T db psql -v ON_ERROR_STOP=1 -U boss -d boss -Atc "SELECT to_regclass('public.identity_vault_keks') IS NOT NULL" </dev/null)"
[[ "$IDENTITY_TABLES" == t || "$IDENTITY_TABLES" == f ]] || { echo 'Could not determine database identity state.' >&2; exit 1; }
if [[ "$IDENTITY_TABLES" == t ]]; then
  "${COMPOSE[@]}" exec -T db psql -v ON_ERROR_STOP=1 -U boss -d boss -Atc \
    "SELECT json_build_object('kcvs', (SELECT coalesce(json_agg(encode(kcv,'hex')), '[]') FROM identity_vault_keks WHERE retired_at IS NULL), 'items', (SELECT count(*) FROM identity_vault_items WHERE status <> 'revoked'))" </dev/null > "$BACKUP/.identity-state.json"
else
  printf '{"kcvs":[],"items":0}\n' > "$BACKUP/.identity-state.json"
fi
# The repos volume is archived inside the same pause of writers as the database dump, so a local
# project's commits and its change-set rows describe the same moment.
ARCHIVE_KINDS=(artifacts identity)
[[ -z "$REPOS_VOLUME" ]] || ARCHIVE_KINDS+=(repos)
for kind in "${ARCHIVE_KINDS[@]}"; do
  "${COMPOSE[@]}" run --rm --no-deps --pull never -T --entrypoint tar api \
    -C "/var/lib/boss/$kind" -cf - . </dev/null > "$BACKUP/$kind.tar"
done
# Parse the entire decompressed custom-format dumps. Never patch binary bytes.
for dump in "$BACKUP/boss.dump" "$BACKUP/infisical.dump"; do
  [[ -f "$dump" ]] || continue
  "${COMPOSE[@]}" exec -T db pg_restore --file=- < "$dump" | python3 "$ARCHIVE_HELPER" scan "$SESSION_KEYS_FILE"
done
python3 - "$BACKUP/.metadata.json" "$BACKUP/.identity-state.json" <<'PYMETA'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
metadata = json.loads(path.read_text())
metadata['identity_state'] = json.loads(Path(sys.argv[2]).read_text())
path.write_text(json.dumps(metadata))
PYMETA
python3 "$ARCHIVE_HELPER" create "$BACKUP" "$BACKUP/.metadata.json" "$SESSION_KEYS_FILE"
rm -f "$SESSION_KEYS_FILE" "$BACKUP/.metadata.json" "$BACKUP/.identity-state.json"
SESSION_KEYS_FILE=""
if [[ "$LEAVE_STOPPED" -eq 1 ]]; then
  if [[ "$DB_STARTED_FOR_BACKUP" -eq 1 ]]; then "${COMPOSE[@]}" stop db </dev/null >&2; fi
else
  restore_services
fi
PAUSED=()
DB_STARTED_FOR_BACKUP=0
trap - EXIT
printf '%s\n' "$BACKUP"
