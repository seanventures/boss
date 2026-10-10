#!/usr/bin/env bash
# Restore a complete private archive into a fresh, explicitly named local stack.
set -euo pipefail
umask 077
trap 'exit 130' INT
trap 'exit 143' TERM
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
HELPER="$SCRIPT_DIR/backup_archive.py"
BACKUP=""; INSTALL_DIR=""; PROJECT=""
usage() {
  echo 'Usage: restore.sh --backup PRIVATE_ARCHIVE --dir FRESH_CHECKOUT --project NEW_PROJECT'
  echo 'Requires matching source and cached images; refuses existing target data. Leaves app services stopped.'
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup) BACKUP="${2:?--backup needs a path}"; shift 2 ;;
    --dir) INSTALL_DIR="${2:?--dir needs a path}"; shift 2 ;;
    --project) PROJECT="${2:?--project needs a name}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ -n "$BACKUP" && -n "$INSTALL_DIR" && "$PROJECT" =~ ^[a-z0-9][a-z0-9_-]+$ ]] || { usage >&2; exit 2; }
BACKUP="$(cd "$BACKUP" && pwd -P)"
INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd -P)"
python3 "$HELPER" validate "$BACKUP"
COMPOSE_FILE="$INSTALL_DIR/scripts/install/community-compose.yml"
[[ -f "$COMPOSE_FILE" ]] || { echo 'The target needs the backed-up source checkout.' >&2; exit 1; }
[[ ! -e "$INSTALL_DIR/.env" && ! -L "$INSTALL_DIR/.env" ]] || { echo 'Target .env already exists; choose a fresh checkout. No target data changed.' >&2; exit 1; }
if [[ -d "$INSTALL_DIR/.git" || -f "$INSTALL_DIR/.git" ]]; then
  SOURCE="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
  git -C "$INSTALL_DIR" diff --quiet && git -C "$INSTALL_DIR" diff --cached --quiet || {
    echo 'Target has tracked changes; use a fresh checkout at the archived revision.' >&2; exit 1;
  }
else
  SOURCE="$(cat "$INSTALL_DIR/.boss-shipped-commit")"
fi
[[ "$SOURCE" == "$(cat "$BACKUP/source-commit")" ]] || { echo 'Target source revision differs from the archive; use its exact source revision.' >&2; exit 1; }
DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then DOCKER=(sudo -n docker)
  else echo 'Docker is unavailable.' >&2; exit 1; fi
fi
# A directory lock protects concurrent invocations for this checkout.
LOCK="$INSTALL_DIR/.boss-restore.lock"
mkdir -m 700 "$LOCK" || { echo 'A restore lock already exists; inspect the previous recovery.' >&2; exit 1; }
TARGET_CREATED=0
# Use an ephemeral loopback database port so the retained original stack can keep its port.
export BOSS_POSTGRES_PORT=0
COMPOSE=()
on_exit() {
  status=$?
  trap - EXIT
  if [[ "$status" -ne 0 ]]; then
    if [[ "$TARGET_CREATED" -eq 1 ]]; then
      if ! "${COMPOSE[@]}" stop </dev/null >/dev/null 2>&1; then
        echo 'Could not stop the recovery database; inspect the named target project.' >&2
      fi
      echo 'Recovery incomplete. Application writers were not started; keep target data for inspection. Original stack was untouched.' >&2
    else
      echo 'Recovery preflight failed; no target data volumes were created.' >&2
    fi
  fi
  rm -rf "$LOCK"
  exit "$status"
}
trap on_exit EXIT
# Render through the archived environment without copying it over anything in the target.
BASE=("${DOCKER[@]}" compose -p "$PROJECT" -f "$COMPOSE_FILE" --env-file "$BACKUP/.env" --profile web --profile secrets)
"${BASE[@]}" config --format json | python3 "$HELPER" layout > "$LOCK/layout.json"
python3 - "$LOCK/layout.json" "$BACKUP/manifest.json" "$LOCK/images.json" <<'PY'
import json, sys
from pathlib import Path
layout, manifest = [json.loads(Path(p).read_text()) for p in sys.argv[1:3]]
if layout['master_key_relative'] != manifest['master_key_relative']:
    raise SystemExit('Target identity key path differs from the archive.')
Path(sys.argv[3]).write_text(json.dumps({'services': {
    'db': {'image': manifest['db_image_id'], 'pull_policy': 'never'},
    'api': {'image': manifest['api_image_id'], 'pull_policy': 'never'},
}}))
PY
for kind in db api; do
  image_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]+"_image_id"])' "$BACKUP/manifest.json" "$kind")"
  "${DOCKER[@]}" image inspect "$image_id" >/dev/null 2>&1 || { echo 'An archived image is unavailable locally. Restore the reviewed image before retrying.' >&2; exit 1; }
done
[[ -z "$("${DOCKER[@]}" ps -aq --filter "label=com.docker.compose.project=$PROJECT")" ]] || { echo 'Target project already has containers; choose a new project name.' >&2; exit 1; }
while IFS= read -r volume; do
  if "${DOCKER[@]}" volume inspect "$volume" >/dev/null 2>&1; then
    echo 'A target volume already exists; choose a new project name. No existing volume was changed.' >&2; exit 1
  fi
done < <(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["all_volumes"]))' "$LOCK/layout.json")
# Parse full dumps with a one-off local image before creating target resources.
DB_IMAGE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["db_image_id"])' "$BACKUP/manifest.json")"
for dump in "$BACKUP/boss.dump" "$BACKUP/infisical.dump"; do
  [[ -f "$dump" ]] || continue
  "${DOCKER[@]}" run --rm --network none --pull never -i --entrypoint pg_restore "$DB_IMAGE" --file=/dev/null < "$dump"
done
# Persist only after all archive/source/image/namespace checks succeeded.
cp "$BACKUP/.env" "$INSTALL_DIR/.env"
chmod 600 "$INSTALL_DIR/.env"
python3 - "$INSTALL_DIR/.env" <<'PYENV'
from pathlib import Path
import sys
p = Path(sys.argv[1])
lines = [line for line in p.read_text().splitlines() if not line.startswith('BOSS_POSTGRES_PORT=')]
p.write_text('\n'.join(lines + ['BOSS_POSTGRES_PORT=0']) + '\n')
PYENV
cp "$LOCK/images.json" "$INSTALL_DIR/.boss-restore-images.json"
OVERRIDE="$INSTALL_DIR/.boss-restore-images.json"
COMPOSE=("${DOCKER[@]}" compose -p "$PROJECT" -f "$COMPOSE_FILE" -f "$OVERRIDE" --env-file "$INSTALL_DIR/.env" --profile web --profile secrets)
TARGET_CREATED=1
for kind in identity artifacts; do
  "${COMPOSE[@]}" run --rm --no-deps --pull never -T --entrypoint tar api \
    -C "/var/lib/boss/$kind" -xf - < "$BACKUP/$kind.tar"
done
"${COMPOSE[@]}" up -d --no-deps --no-build --pull never db </dev/null
ready=0
for attempt in {1..60}; do
  if "${COMPOSE[@]}" exec -T db pg_isready -h 127.0.0.1 -U boss -d boss </dev/null >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
[[ "$ready" -eq 1 ]] || { echo 'Restored target database did not become ready.' >&2; exit 1; }
for name in boss infisical; do
  [[ -f "$BACKUP/$name.dump" ]] || continue
  if [[ "$name" == infisical && "$("${COMPOSE[@]}" exec -T db psql -U boss -d boss -Atc "SELECT 1 FROM pg_database WHERE datname='infisical'" </dev/null)" != 1 ]]; then
    "${COMPOSE[@]}" exec -T db createdb -U boss infisical </dev/null
  fi
  "${COMPOSE[@]}" exec -T db pg_restore -U boss -d "$name" --exit-on-error --single-transaction --no-owner < "$BACKUP/$name.dump"
done
# Recheck database key metadata after actual restore, before any application can boot.
IDENTITY_TABLES="$("${COMPOSE[@]}" exec -T db psql -v ON_ERROR_STOP=1 -U boss -d boss -Atc "SELECT to_regclass('public.identity_vault_keks') IS NOT NULL" </dev/null)"
[[ "$IDENTITY_TABLES" == t || "$IDENTITY_TABLES" == f ]] || { echo 'Could not determine database identity state.' >&2; exit 1; }
if [[ "$IDENTITY_TABLES" == t ]]; then
  "${COMPOSE[@]}" exec -T db psql -v ON_ERROR_STOP=1 -U boss -d boss -Atc \
    "SELECT json_build_object('kcvs', (SELECT coalesce(json_agg(encode(kcv,'hex')), '[]') FROM identity_vault_keks WHERE retired_at IS NULL), 'items', (SELECT count(*) FROM identity_vault_items WHERE status <> 'revoked'))" </dev/null > "$LOCK/restored-identity.json"
else
  printf '{"kcvs":[],"items":0}\n' > "$LOCK/restored-identity.json"
fi
python3 - "$LOCK/restored-identity.json" "$BACKUP/manifest.json" <<'PY'
import json, sys
from pathlib import Path
actual = json.loads(Path(sys.argv[1]).read_text())
expected = json.loads(Path(sys.argv[2]).read_text())['identity_state']
if actual['items'] != expected['items'] or sorted(actual['kcvs']) != sorted(expected['kcvs']):
    raise SystemExit('Restored database identity metadata differs from the archive; application must stay stopped.')
PY
printf 'Recovery data restored into project %s. Application services remain stopped.\n' "$PROJECT"
printf 'Choose a free API port in the target .env. Start only the archived API image for recovery verification:\n'
printf '%q ' "${COMPOSE[@]}"
printf 'up -d --no-deps --no-build --pull never api\n'
