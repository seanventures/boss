#!/usr/bin/env bash
# Back up a running Community VM before re-running the current upstream installer.
set -euo pipefail
umask 077

INSTALL_DIR="${BOSS_INSTALL_DIR:-$HOME/boss}"
REF="${BOSS_REPO_REF:-main}"
BACKUP_DIR=""
WEB_MODE=""
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: upgrade.sh [--dir INSTALL_DIR] [--ref BRANCH] [--backup-dir PATH]
                  [--web | --no-web] [--dry-run]

Fetch a fast-forward update, back up .env/Postgres/artifacts, then run the
fetched installer. Existing credentials and data volumes are retained.
The backup path is printed before any source or container change.
The installer remembers whether Mission Control was enabled. For installs
created before that setting was recorded, pass --no-web once to keep it off.
Use --web or --no-web to change the existing choice deliberately.
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="${2:?--dir needs a path}"; shift 2 ;;
    --ref) REF="${2:?--ref needs a branch}"; shift 2 ;;
    --backup-dir) BACKUP_DIR="${2:?--backup-dir needs a path}"; shift 2 ;;
    --no-web|--web)
      [[ -z "$WEB_MODE" ]] || { echo 'Choose only one of --web and --no-web.' >&2; exit 2; }
      WEB_MODE="$1"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done
INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd -P)"
[[ -f "$INSTALL_DIR/.env" && -f "$INSTALL_DIR/scripts/install/community-compose.yml" ]] ||
  { echo 'No BOSS Community install with private .env found; nothing changed.' >&2; exit 1; }
[[ "$(git -C "$INSTALL_DIR" rev-parse --is-inside-work-tree 2>/dev/null || true)" == true ]] ||
  { echo 'This install is a remote.sh archive without Git; run remote.sh update from the operator machine.' >&2; exit 1; }
[[ -z "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=no)" ]] ||
  { echo 'Tracked local changes need to be committed or moved before upgrade; nothing changed.' >&2; exit 1; }

OLD_COMMIT="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
git -C "$INSTALL_DIR" fetch origin "$REF" </dev/null
NEW_COMMIT="$(git -C "$INSTALL_DIR" rev-parse FETCH_HEAD)"
git -C "$INSTALL_DIR" merge-base --is-ancestor "$OLD_COMMIT" "$NEW_COMMIT" ||
  { echo 'Upstream cannot fast-forward this checkout; nothing changed.' >&2; exit 1; }
git -C "$INSTALL_DIR" cat-file -e FETCH_HEAD:scripts/install/backup.sh 2>/dev/null ||
  { echo 'Target ref has no backup helper; source and containers were not changed.' >&2; exit 1; }
[[ "$OLD_COMMIT" != "$NEW_COMMIT" ]] || echo 'Already at the requested source commit; verifying with a safe installer rerun.'

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf 'Would back up %s, then rerun the installer from %s (%s → %s).\n' "$INSTALL_DIR" "$REF" "$OLD_COMMIT" "$NEW_COMMIT"
  exit 0
fi

FETCHED_BACKUP="$(mktemp)"
FETCHED_INSTALLER="$(mktemp)"
trap 'rm -f "$FETCHED_BACKUP" "$FETCHED_INSTALLER"' EXIT
git -C "$INSTALL_DIR" show FETCH_HEAD:scripts/install/backup.sh > "$FETCHED_BACKUP"
git -C "$INSTALL_DIR" show FETCH_HEAD:install.sh > "$FETCHED_INSTALLER"
backup_args=(--dir "$INSTALL_DIR")
[[ -n "$BACKUP_DIR" ]] && backup_args+=(--backup-dir "$BACKUP_DIR")
BACKUP="$(bash "$FETCHED_BACKUP" "${backup_args[@]}" </dev/null)" || {
  echo 'Backup failed; source and containers were not changed.' >&2; exit 1;
}
printf 'Private recovery backup: %s\n' "$BACKUP"

# The fetched installer performs its own fast-forward and credential-preserving
# rerun. Execute that version so new installer fixes apply during this upgrade.
install_args=(--dir "$INSTALL_DIR" --ref "$REF" --yes --skip-model-key)
[[ -n "$WEB_MODE" ]] && install_args+=("$WEB_MODE")
if ! bash "$FETCHED_INSTALLER" "${install_args[@]}" </dev/null; then
  echo "Upgrade failed. Recovery backup: $BACKUP" >&2
  echo 'Inspect Compose logs before retrying. See scripts/install/README.md for the recovery procedure.' >&2
  exit 1
fi
printf 'Upgrade complete: %s → %s\nRecovery backup: %s\n' "$OLD_COMMIT" "$NEW_COMMIT" "$BACKUP"
