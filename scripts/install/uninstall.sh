#!/usr/bin/env bash
# Stop a Community install while retaining everything needed to reinstall it.
set -euo pipefail
umask 077

INSTALL_DIR="${BOSS_INSTALL_DIR:-}"
ASSUME_YES=0
DRY_RUN=0
PURGE_DATA=0
SKIP_BACKUP=0
BACKUP_DIR=""
CONFIRM_PURGE=""

usage() {
  cat <<'EOF'
Usage: uninstall.sh [--dir PATH] [--yes] [--dry-run]
                    [--purge-data] [--backup-dir PATH] [--skip-backup]
                    [--confirm-purge DELETE-BOSS-DATA]

Default: stop this Compose stack's containers. Keep them, their exact images,
the checkout, private .env, database, artifacts, CLI sessions, and backups.

--dry-run      show the intended action without changing Docker or files
--yes          skip the default safe-uninstall prompt
--purge-data   also remove this stack's named volumes and private .env
--backup-dir   where to place a private backup before data purge
--skip-backup  allow purge without a backup
--confirm-purge DELETE-BOSS-DATA
               explicit noninteractive purge confirmation; --yes alone never purges

Data purge does not remove backups or the source checkout. Without
--skip-backup, the database must be running so it can be backed up first.
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="${2:?--dir needs a path}"; shift 2 ;;
    --backup-dir) BACKUP_DIR="${2:?--backup-dir needs a path}"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --purge-data) PURGE_DATA=1; shift ;;
    --skip-backup) SKIP_BACKUP=1; shift ;;
    --confirm-purge) CONFIRM_PURGE="${2:?--confirm-purge needs a value}"; shift 2 ;;
    --keep-env|--keep-images) shift ;; # Older callers: both are now the default.
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done
if [[ -z "$INSTALL_DIR" ]]; then
  [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]] || {
    echo 'A piped uninstall needs --dir PATH or BOSS_INSTALL_DIR.' >&2; exit 2;
  }
  INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fi
[[ "$SKIP_BACKUP" -eq 0 || "$PURGE_DATA" -eq 1 ]] || { echo '--skip-backup requires --purge-data.' >&2; exit 2; }
[[ -z "$CONFIRM_PURGE" || "$PURGE_DATA" -eq 1 ]] || { echo '--confirm-purge requires --purge-data.' >&2; exit 2; }
[[ -z "$CONFIRM_PURGE" || "$CONFIRM_PURGE" == 'DELETE-BOSS-DATA' ]] || { echo 'Incorrect purge confirmation; nothing was removed.' >&2; exit 2; }
INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd -P)"
COMPOSE_FILE="$INSTALL_DIR/scripts/install/community-compose.yml"
ENV_FILE="$INSTALL_DIR/.env"
[[ -f "$COMPOSE_FILE" && -f "$ENV_FILE" ]] || { echo 'Community Compose file and .env are required; nothing was removed.' >&2; exit 1; }

if [[ "$DRY_RUN" -eq 1 ]]; then
  if [[ "$PURGE_DATA" -eq 1 ]]; then
    echo 'Would back up the running database and artifacts (unless --skip-backup), then remove this Compose stack, its named volumes, .env, and env shim.'
  else
    echo 'Would stop this Compose stack; containers, .env, volumes, images, backups, and checkout remain.'
  fi
  exit 0
fi

if [[ "$PURGE_DATA" -eq 1 ]]; then
  if [[ "$CONFIRM_PURGE" != 'DELETE-BOSS-DATA' ]]; then
    if ! (: </dev/tty) 2>/dev/null; then
      echo 'Data purge needs a terminal confirmation or --confirm-purge DELETE-BOSS-DATA; nothing was removed.' >&2
      exit 1
    fi
    printf 'This removes the BOSS database, artifacts, CLI sessions, and .env. Type DELETE-BOSS-DATA: ' >/dev/tty
    read -r CONFIRM_PURGE </dev/tty || true
    [[ "$CONFIRM_PURGE" == 'DELETE-BOSS-DATA' ]] || { echo 'Confirmation did not match; nothing was removed.' >&2; exit 1; }
  fi
elif [[ "$ASSUME_YES" -eq 0 ]]; then
  if ! (: </dev/tty) 2>/dev/null; then
    echo 'Pass --yes for a noninteractive safe uninstall; nothing was removed.' >&2; exit 1
  fi
  printf 'Stop BOSS Community? Data and credentials will be kept. [y/N] ' >/dev/tty
  read -r reply </dev/tty || true
  [[ "$reply" == y || "$reply" == Y || "$reply" == yes ]] || { echo 'Nothing was removed.'; exit 1; }
fi

DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker)
  else
    echo 'Docker daemon is unavailable; nothing was removed.' >&2; exit 1
  fi
fi
# The images are tagged with the compose project name (install.sh), not "local".
export BOSS_COMMUNITY_IMAGE_TAG="${BOSS_COMMUNITY_IMAGE_TAG:-${COMPOSE_PROJECT_NAME:-boss-community}}"
COMPOSE=("${DOCKER[@]}" compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" --profile web --profile secrets)
if [[ "$PURGE_DATA" -eq 1 && "$SKIP_BACKUP" -eq 0 ]]; then
  args=(--dir "$INSTALL_DIR")
  [[ -n "$BACKUP_DIR" ]] && args+=(--backup-dir "$BACKUP_DIR")
  backup="$(bash "$INSTALL_DIR/scripts/install/backup.sh" "${args[@]}" </dev/null)" || {
    echo 'Backup failed; data purge was cancelled.' >&2; exit 1;
  }
  printf 'Private backup: %s\n' "$backup"
fi

if [[ "$PURGE_DATA" -eq 1 ]]; then
  "${COMPOSE[@]}" down --volumes --remove-orphans </dev/null
  rm -f "$ENV_FILE" "$INSTALL_DIR/boss-community.env.sh"
  echo 'BOSS Community data purged. Source checkout and any backups remain.'
else
  "${COMPOSE[@]}" stop </dev/null
  echo 'BOSS Community stopped. Containers, .env, volumes, images, backups, and checkout were kept.'
  printf 'Reinstall: cd %s && ./install.sh --dir "%s" --yes --skip-model-key\n' "$INSTALL_DIR" "$INSTALL_DIR"
fi
