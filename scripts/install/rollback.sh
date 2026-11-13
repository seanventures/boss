#!/usr/bin/env bash
# Put the release that ran before the last upgrade back, from its kept images, without a build.
set -euo pipefail
umask 077
trap 'exit 130' INT
trap 'exit 143' TERM

INSTALL_DIR="${BOSS_INSTALL_DIR:-$HOME/boss}"
RESTORE_BACKUP=""
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: rollback.sh [--dir INSTALL_DIR] [--restore-backup BACKUP_DIR] [--dry-run]

`boss upgrade` keeps the images that ran before it as prev-<tag> and records them in
.boss-rollback. This puts that release back in seconds: the source checkout returns to the
previous commit, the image tags point at the kept images again, and the running services are
recreated from them. Nothing is built or pulled.

The new release may have changed the database schema. When db/schema.sql differs from the
one the previous images were built with, this refuses, because the previous release would run
against a schema it does not know. Pass --restore-backup with the backup the upgrade made
(its path was printed before the upgrade changed anything): the database is then put back to
that moment first. The backup is restored into a new database and swapped in only when that
succeeds; the database it replaces is kept, renamed boss_before_rollback_<time>, with the work
done since the upgrade.

--dry-run says what would happen and changes nothing.
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="${2:?--dir needs a path}"; shift 2 ;;
    --restore-backup) RESTORE_BACKUP="${2:?--restore-backup needs a backup directory}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done

INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd -P)"
ENV_FILE="$INSTALL_DIR/.env"
COMPOSE_FILE="$INSTALL_DIR/scripts/install/community-compose.yml"
MARKER="$INSTALL_DIR/.boss-rollback"
[[ -f "$ENV_FILE" && -f "$COMPOSE_FILE" ]] ||
  { echo 'No BOSS Community install with private .env found; nothing changed.' >&2; exit 1; }
[[ -f "$MARKER" ]] ||
  { echo 'No rollback point: .boss-rollback is written by boss upgrade (from this release on). Nothing changed.' >&2; exit 1; }
[[ "$(git -C "$INSTALL_DIR" rev-parse --is-inside-work-tree 2>/dev/null || true)" == true ]] ||
  { echo 'This install is a remote.sh archive without Git; roll back with remote.sh from the operator machine.' >&2; exit 1; }

marker_value() { grep -m 1 "^$1=" "$MARKER" | cut -d= -f2- || true; }
PREV_COMMIT="$(marker_value BOSS_PREV_COMMIT)"
PREV_REF="$(marker_value BOSS_PREV_REF)"
TAG="$(marker_value BOSS_IMAGE_TAG)"
PREV_TAG="$(marker_value BOSS_PREV_IMAGE_TAG)"
PREV_IMAGES="$(marker_value BOSS_PREV_IMAGES)"
PREV_SCHEMA="$(marker_value BOSS_PREV_SCHEMA_SHA256)"
[[ "$PREV_COMMIT" =~ ^[0-9a-f]{40}$ && -n "$TAG" && "$PREV_TAG" == "prev-$TAG" && -n "$PREV_IMAGES" ]] ||
  { echo '.boss-rollback is incomplete; nothing changed.' >&2; exit 1; }
[[ "$TAG" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || { echo '.boss-rollback names an invalid image tag; nothing changed.' >&2; exit 1; }
CURRENT_COMMIT="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
[[ "$CURRENT_COMMIT" != "$PREV_COMMIT" ]] ||
  { echo "Already on the previous release ($PREV_COMMIT); nothing to roll back." >&2; exit 1; }
git -C "$INSTALL_DIR" cat-file -e "$PREV_COMMIT^{commit}" 2>/dev/null ||
  { echo "The previous commit $PREV_COMMIT is not in this checkout; nothing changed." >&2; exit 1; }
[[ -z "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=no)" ]] ||
  { echo 'Tracked local changes need to be committed or moved before rollback; nothing changed.' >&2; exit 1; }

if docker info >/dev/null 2>&1; then DOCKER=(docker)
elif command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then DOCKER=(sudo -n docker)
else echo 'Docker is unavailable to this user; nothing changed.' >&2; exit 1; fi

# Every kept image must still exist: a rollback that starts half the services is worse than none.
read -r -a IMAGES <<<"$PREV_IMAGES"
for repo in "${IMAGES[@]}"; do
  [[ "$repo" =~ ^boss-community-[a-z0-9-]+$ ]] || { echo ".boss-rollback names an unexpected image $repo; nothing changed." >&2; exit 1; }
  "${DOCKER[@]}" image inspect "$repo:$PREV_TAG" >/dev/null 2>&1 ||
    { echo "The kept image $repo:$PREV_TAG is gone; instant rollback is not possible. Use boss upgrade --to $PREV_REF instead. Nothing changed." >&2; exit 1; }
done

# THE SCHEMA GUARD. The running database was converged to the CURRENT checkout's schema.sql when
# the new release started. When that differs from the previous release's, the previous images
# would run against a schema they do not know; only the pre-upgrade backup makes that safe.
sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}
CURRENT_SCHEMA="$(sha256_file "$INSTALL_DIR/db/schema.sql")"
SCHEMA_NEWER=0
[[ -n "$PREV_SCHEMA" && "$PREV_SCHEMA" == "$CURRENT_SCHEMA" ]] || SCHEMA_NEWER=1
if [[ "$SCHEMA_NEWER" -eq 1 && -z "$RESTORE_BACKUP" ]]; then
  {
    echo "The running database schema is newer than the previous release ($PREV_REF) expects:"
    echo "db/schema.sql changed in the upgrade. Rolling back onto it could break the previous release."
    echo "Pass --restore-backup PATH with the backup the upgrade made (printed before it changed"
    echo "anything, under ~/boss-backups by default) to restore the pre-upgrade database first."
    echo "Nothing changed."
  } >&2
  exit 1
fi
if [[ -n "$RESTORE_BACKUP" ]]; then
  RESTORE_BACKUP="$(cd "$RESTORE_BACKUP" && pwd -P)"
  [[ -f "$RESTORE_BACKUP/boss.dump" && -f "$RESTORE_BACKUP/source-commit" ]] ||
    { echo 'That is not a BOSS backup (no boss.dump and source-commit); nothing changed.' >&2; exit 1; }
  if [[ -f "$INSTALL_DIR/scripts/install/backup_archive.py" ]] && command -v python3 >/dev/null 2>&1; then
    python3 "$INSTALL_DIR/scripts/install/backup_archive.py" validate "$RESTORE_BACKUP" >/dev/null ||
      { echo 'The backup does not validate; nothing changed.' >&2; exit 1; }
  fi
  [[ "$(cat "$RESTORE_BACKUP/source-commit")" == "$PREV_COMMIT" ]] ||
    { echo "That backup was taken on $(cat "$RESTORE_BACKUP/source-commit"), not on the previous release $PREV_COMMIT; nothing changed." >&2; exit 1; }
fi

export BOSS_COMMUNITY_IMAGE_TAG="$TAG"
COMPOSE=("${DOCKER[@]}" compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE")
RUNNING="$("${COMPOSE[@]}" --profile web --profile secrets --profile mail ps --status running --services 2>/dev/null || true)"
SERVICES=()
while IFS= read -r service; do
  [[ -z "$service" || "$service" == db ]] || SERVICES+=("$service")
done <<<"$RUNNING"
[[ ${#SERVICES[@]} -gt 0 ]] || SERVICES=(api worker)

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf 'Would roll back %s → %s (%s).\n' "$CURRENT_COMMIT" "$PREV_COMMIT" "${PREV_REF:-previous release}"
  for repo in "${IMAGES[@]}"; do printf '  would point %s:%s at %s:%s\n' "$repo" "$TAG" "$repo" "$PREV_TAG"; done
  if [[ -n "$RESTORE_BACKUP" ]]; then
    printf '  would restore %s/boss.dump into a new database and swap it in (the current one kept, renamed)\n' "$RESTORE_BACKUP"
  else
    printf '  the schema is unchanged since the previous release; the database stays as it is\n'
  fi
  printf '  would recreate without building: %s\n' "${SERVICES[*]}"
  exit 0
fi

# 1. The database, only when the schema moved and the operator gave the pre-upgrade backup. The
#    backup is restored into a NEW database first; only when that succeeds are the two swapped,
#    so a restore that fails leaves the running database exactly as it was. The replaced one is
#    kept, renamed, with the work done since the upgrade.
if [[ -n "$RESTORE_BACKUP" ]]; then
  SAFETY_DB="boss_before_rollback_$(date -u +%Y%m%d%H%M%S)"
  PSQL=("${COMPOSE[@]}" exec -T db psql -v ON_ERROR_STOP=1 -U boss -d postgres -q)
  restart_and_fail() {
    "${PSQL[@]}" -c 'DROP DATABASE IF EXISTS boss_rollback_restore WITH (FORCE)' </dev/null >&2 || true
    "${COMPOSE[@]}" start "${SERVICES[@]}" </dev/null >&2 || true
    echo "$1 The database, source and images were not changed; BOSS runs as before." >&2
    exit 1
  }
  echo 'Stopping BOSS services to restore the database...' >&2
  "${COMPOSE[@]}" stop "${SERVICES[@]}" </dev/null >&2
  "${PSQL[@]}" -c 'DROP DATABASE IF EXISTS boss_rollback_restore WITH (FORCE)' \
    -c 'CREATE DATABASE boss_rollback_restore OWNER boss' </dev/null >&2 ||
    restart_and_fail 'Could not create the restore database.'
  "${COMPOSE[@]}" exec -T db pg_restore -U boss -d boss_rollback_restore --exit-on-error \
    --single-transaction --no-owner < "$RESTORE_BACKUP/boss.dump" >&2 ||
    restart_and_fail "The backup's database did not restore."
  "${PSQL[@]}" \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'boss' AND pid <> pg_backend_pid()" \
    -c "ALTER DATABASE boss RENAME TO $SAFETY_DB" \
    -c 'ALTER DATABASE boss_rollback_restore RENAME TO boss' </dev/null >/dev/null ||
    restart_and_fail 'Could not swap the restored database in.'
  echo "Database restored from $RESTORE_BACKUP; the one it replaced is kept as $SAFETY_DB." >&2
fi

# 2. Source: the checkout the previous images were built from (compose file, scripts, schema).
if git -C "$INSTALL_DIR" symbolic-ref -q HEAD >/dev/null 2>&1; then
  git -C "$INSTALL_DIR" reset -q --hard "$PREV_COMMIT" </dev/null
else
  git -C "$INSTALL_DIR" checkout -q --detach "$PREV_COMMIT" </dev/null
fi

# 3. Images: the canonical tag points at the kept images again (the newer ones stay as
#    rolled-back-<tag> until prune), then the services are recreated from them. No build.
for repo in "${IMAGES[@]}"; do
  if "${DOCKER[@]}" image inspect "$repo:$TAG" >/dev/null 2>&1; then
    "${DOCKER[@]}" tag "$repo:$TAG" "$repo:rolled-back-$TAG" </dev/null
  fi
  "${DOCKER[@]}" tag "$repo:$PREV_TAG" "$repo:$TAG" </dev/null
done
"${COMPOSE[@]}" up -d --no-build --pull never --no-deps --force-recreate "${SERVICES[@]}" </dev/null >&2

# 4. The install record follows the release that runs.
if [[ -f "$INSTALL_DIR/.boss-release" && -n "$PREV_REF" ]]; then
  tmp="$INSTALL_DIR/.boss-release.writing.$$"
  awk -v ref="$PREV_REF" -v commit="$PREV_COMMIT" '
    /^BOSS_RELEASE_REF=/ { print "BOSS_RELEASE_REF=" ref; next }
    /^BOSS_INSTALLED_COMMIT=/ { print "BOSS_INSTALLED_COMMIT=" commit; next }
    { print }' "$INSTALL_DIR/.boss-release" > "$tmp"
  mv -f "$tmp" "$INSTALL_DIR/.boss-release"
fi
mv -f "$MARKER" "$MARKER.used"
printf 'Rolled back to %s (%s). No build ran.\n' "${PREV_REF:-$PREV_COMMIT}" "$PREV_COMMIT"
