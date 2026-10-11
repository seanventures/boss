#!/usr/bin/env bash
# Restore a complete private archive into a fresh, explicitly named local stack.
set -euo pipefail
umask 077
trap 'exit 130' INT
trap 'exit 143' TERM
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
HELPER="$SCRIPT_DIR/backup_archive.py"
RECOVERY_HELPER="$SCRIPT_DIR/recovery_compose.py"
BACKUP=""; INSTALL_DIR=""; PROJECT=""
OFFLINE=0
usage() {
  echo 'Usage: restore.sh --backup PRIVATE_ARCHIVE --dir FRESH_CHECKOUT --project NEW_PROJECT [--offline-data-only]'
  echo 'Requires matching guarded source/images for app recovery. Older archives require explicit DB-only offline recovery.'
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup) BACKUP="${2:?--backup needs a path}"; shift 2 ;;
    --dir) INSTALL_DIR="${2:?--dir needs a path}"; shift 2 ;;
    --project) PROJECT="${2:?--project needs a name}"; shift 2 ;;
    --offline-data-only) OFFLINE=1; shift ;;
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
for path in "$INSTALL_DIR/scripts" "$INSTALL_DIR/scripts/install" "$COMPOSE_FILE"; do
  [[ ! -L "$path" ]] || { echo 'Recovery refuses linked source paths; choose a separate fresh checkout.' >&2; exit 1; }
done
[[ ! -e "$INSTALL_DIR/.env" && ! -L "$INSTALL_DIR/.env" ]] || { echo 'Target .env already exists; choose a fresh checkout. No target data changed.' >&2; exit 1; }
for path in "$INSTALL_DIR/.boss-restore-images.json" "$INSTALL_DIR/.boss-archived-compose.yml"; do
  [[ ! -e "$path" && ! -L "$path" ]] || { echo 'Target has prior recovery files; preserve them and choose a fresh checkout.' >&2; exit 1; }
done
if [[ -d "$INSTALL_DIR/.git" || -f "$INSTALL_DIR/.git" ]]; then
  SOURCE="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
  git -C "$INSTALL_DIR" diff --quiet && git -C "$INSTALL_DIR" diff --cached --quiet || {
    echo 'Target has tracked changes; use a fresh checkout at the archived revision.' >&2; exit 1;
  }
else
  SOURCE="$(cat "$INSTALL_DIR/.boss-shipped-commit")"
fi
[[ "$SOURCE" == "$(cat "$BACKUP/source-commit")" ]] || { echo 'Target source revision differs from the archive; use its exact source revision.' >&2; exit 1; }
if [[ "$OFFLINE" -eq 0 ]]; then
  python3 "$RECOVERY_HELPER" source "$INSTALL_DIR" >/dev/null
fi
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
  boss_sudo_scope_safe "$BACKUP/.env" restore || { echo "Cannot safely use sudo for this Docker selection. Use direct Docker access for custom projects, contexts or connection settings." >&2; exit 1; }
  if command -v sudo >/dev/null 2>&1 && sudo -n docker --host unix:///var/run/docker.sock info >/dev/null 2>&1; then DOCKER=(sudo -n docker --host unix:///var/run/docker.sock)
  else echo 'Docker is unavailable.' >&2; exit 1; fi
fi
# A directory lock protects concurrent invocations for this checkout.
LOCK="$INSTALL_DIR/.boss-restore.lock"
mkdir -m 700 "$LOCK" || { echo 'A restore lock already exists; inspect the previous recovery.' >&2; exit 1; }
TARGET_CREATED=0
HOLD_OWNED=0
DATABASE_ATTEMPTED=0
# Use an ephemeral loopback database port so the retained original stack can keep its port.
export BOSS_POSTGRES_PORT=0
COMPOSE=()
on_exit() {
  status=$?
  trap - EXIT
  if [[ "$status" -ne 0 ]]; then
    if [[ "$TARGET_CREATED" -eq 1 ]]; then
      if [[ "$HOLD_OWNED" -eq 1 && "$DATABASE_ATTEMPTED" -eq 1 ]] && ! "${COMPOSE[@]}" stop </dev/null >/dev/null 2>&1; then
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
"${BASE[@]}" config --format json > "$LOCK/config.json"
python3 "$HELPER" layout < "$LOCK/config.json" > "$LOCK/layout.json"
python3 - "$LOCK/layout.json" "$BACKUP/manifest.json" <<'PY'
import json, sys
from pathlib import Path
layout, manifest = [json.loads(Path(p).read_text()) for p in sys.argv[1:3]]
if layout['master_key_relative'] != manifest['master_key_relative']:
    raise SystemExit('Target identity key path differs from the archive.')
PY
for kind in db api; do
  image_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]+"_image_id"])' "$BACKUP/manifest.json" "$kind")"
  "${DOCKER[@]}" image inspect "$image_id" >/dev/null 2>&1 || { echo 'An archived image is unavailable locally. Restore the reviewed image before retrying.' >&2; exit 1; }
done
DB_IMAGE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["db_image_id"])' "$BACKUP/manifest.json")"
API_IMAGE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["api_image_id"])' "$BACKUP/manifest.json")"
if [[ "$OFFLINE" -eq 0 ]]; then
  # Only selected public metadata is captured, never image environment values.
  "${DOCKER[@]}" image inspect --format '{{.Id}}|{{index .Config.Labels "io.boss.recovery-guard"}}|{{index .Config.Labels "org.opencontainers.image.revision"}}|{{json .Config.Entrypoint}}|{{.Config.User}}' "$API_IMAGE" > "$LOCK/image-metadata.txt"
  python3 - "$LOCK/image-metadata.txt" "$LOCK/image-metadata.json" <<'PY'
import json, sys
from pathlib import Path
values = Path(sys.argv[1]).read_text().strip().split('|')
if len(values) != 5:
    raise SystemExit('Archived API image guard metadata is unavailable.')
metadata = dict(zip(('image_id', 'interface', 'source_commit'), values[:3]))
metadata.update(entrypoint=json.loads(values[3]), user=values[4])
Path(sys.argv[2]).write_text(json.dumps(metadata))
PY
  # Override the entrypoint only for an offline file-hash probe, never app startup.
  "${DOCKER[@]}" run --rm --network none --pull never --read-only --user 0:0 --entrypoint python3 "$API_IMAGE" -c \
    'import hashlib,json; from pathlib import Path; paths=["/usr/local/libexec/boss/recovery_guard.py","/usr/local/bin/boss-entrypoint"]; print(json.dumps({p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},sort_keys=True))' > "$LOCK/image-hashes.json"
  python3 "$RECOVERY_HELPER" image "$INSTALL_DIR" "$BACKUP/manifest.json" "$LOCK/image-metadata.json" "$LOCK/image-hashes.json"
fi
PREPARE=(python3 "$RECOVERY_HELPER" prepare "$LOCK/config.json" "$LOCK/layout.json" "$BACKUP/manifest.json" "$LOCK" "$PROJECT")
[[ "$OFFLINE" -eq 0 ]] || PREPARE+=(--offline-data-only)
"${PREPARE[@]}"
[[ -z "$("${DOCKER[@]}" ps -aq --filter "label=com.docker.compose.project=$PROJECT")" ]] || { echo 'Target project already has containers; choose a new project name.' >&2; exit 1; }
while IFS= read -r volume; do
  if "${DOCKER[@]}" volume inspect "$volume" >/dev/null 2>&1; then
    echo 'A target volume already exists; choose a new project name. No existing volume was changed.' >&2; exit 1
  fi
done < <(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["all_volumes"]))' "$LOCK/layout.json")
# Parse full dumps with a one-off local image before creating target resources.
for dump in "$BACKUP/boss.dump" "$BACKUP/infisical.dump"; do
  [[ -f "$dump" ]] || continue
  "${DOCKER[@]}" run --rm --network none --pull never -i --entrypoint pg_restore "$DB_IMAGE" --file=/dev/null < "$dump"
done
# Prepare privately: a usable target .env must never precede the held marker.
cp "$BACKUP/.env" "$LOCK/recovery.env"
chmod 600 "$LOCK/recovery.env"
python3 "$RECOVERY_HELPER" env "$LOCK/recovery.env" "$BACKUP/manifest.json" "$PROJECT"
PLANNED_BASE="$COMPOSE_FILE"
[[ "$OFFLINE" -eq 0 ]] || PLANNED_BASE="$LOCK/offline-compose.json"
# Confirm the planned base alone retains pins before creating target resources.
"${DOCKER[@]}" compose -p "$PROJECT" -f "$PLANNED_BASE" --env-file "$LOCK/recovery.env" --profile web --profile secrets config --format json > "$LOCK/pinned-base.json"
python3 - "$LOCK/pinned-base.json" "$BACKUP/manifest.json" "$OFFLINE" <<'PY'
import json, sys
from pathlib import Path
config, manifest = [json.loads(Path(p).read_text()) for p in sys.argv[1:3]]
expected = {'db': manifest['db_image_id']}
if sys.argv[3] == '0':
    expected.update({role: manifest['api_image_id'] for role in ('api', 'worker', 'egress', 'mcp-tools')})
elif set(config['services']) != {'db'}:
    raise SystemExit('Offline recovery base must contain only the database.')
if any(config['services'][role].get('image') != image for role, image in expected.items()):
    raise SystemExit('The base Compose does not preserve the immutable recovery image pins.')
PY
RECOVERY_VOLUME="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["recovery_volume"])' "$LOCK/layout.json")"
TARGET_CREATED=1
create_owned_volume() {
  local kind="$1" logical name labels
  logical="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]]["logical"])' "$LOCK/volume-plan.json" "$kind")"
  name="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]]["name"])' "$LOCK/volume-plan.json" "$kind")"
  "${DOCKER[@]}" volume create --label "com.docker.compose.project=$PROJECT" \
    --label "com.docker.compose.volume=$logical" "$name" >/dev/null
  labels="$("${DOCKER[@]}" volume inspect --format '{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.docker.compose.volume"}}' "$name")"
  [[ "$labels" == "$PROJECT|$logical" ]] || { echo 'Recovery volume ownership does not match; refusing data writes.' >&2; return 1; }
}
# Write the independent hold before restoring data. No application role runs.
create_owned_volume recovery
"${DOCKER[@]}" run --rm --network none --pull never --user 0:0 -i \
  --mount "type=volume,src=$RECOVERY_VOLUME,dst=/recovery" --entrypoint sh "$DB_IMAGE" \
  -c 'set -euC; test ! -L /recovery/hold.json; chmod 0755 /recovery; umask 077; cat > /recovery/hold.json; chmod 0444 /recovery/hold.json' < "$LOCK/hold.json"
HOLD_OWNED=1
# Only now publish startup configuration. Offline base becomes DB-only before
# the complete canonical .env is atomically visible; normal base is guarded.
if [[ "$OFFLINE" -eq 1 ]]; then
  cp "$COMPOSE_FILE" "$INSTALL_DIR/.boss-archived-compose.yml"
  cp "$LOCK/offline-compose.json" "$COMPOSE_FILE"
fi
cp "$LOCK/images.json" "$INSTALL_DIR/.boss-restore-images.json"
python3 - "$LOCK/recovery.env" "$INSTALL_DIR/.env" <<'PY'
import os, sys
# Same filesystem, exclusive publication; never overwrite another target .env.
os.link(sys.argv[1], sys.argv[2])
PY
OVERRIDE="$INSTALL_DIR/.boss-restore-images.json"
COMPOSE=("${DOCKER[@]}" compose -p "$PROJECT" -f "$COMPOSE_FILE" -f "$OVERRIDE" --env-file "$INSTALL_DIR/.env" --profile web --profile secrets)
for kind in identity artifacts; do
  create_owned_volume "$kind"
  DATA_VOLUME="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]+"_volume"])' "$LOCK/layout.json" "$kind")"
  "${DOCKER[@]}" run --rm --network none --pull never --user 0:0 -i \
    --mount "type=volume,src=$DATA_VOLUME,dst=/restore" --entrypoint tar "$API_IMAGE" \
    -C /restore -xf - < "$BACKUP/$kind.tar"
done
DATABASE_ATTEMPTED=1
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
printf 'Recovery data restored into project %s. Application startup remains HELD.\n' "$PROJECT"
if [[ "$OFFLINE" -eq 1 ]]; then
  printf 'Offline data-only recovery: base Compose declares only the database. Archived application source is retained separately.\n'
fi
printf 'Preserve the recovery volume, originals and provider evidence. Offline data/key checks do not verify Owner sign-in or permission to resume.\n'
printf 'Provider-specific reconciliation and a reviewed recovery plan are required; no application restart is authorized.\n'
