#!/usr/bin/env bash
# Free disk BOSS used, and only what BOSS made (stability spec §4a.8; storage hygiene layer 1,
# BOSSAPP-1147).
#
# Every deploy builds new images and leaves build cache behind; nothing removed any of it.
# boss-1 reached 80% disk with 46.5 GB of build cache and 8 GB of stale `:local` tags; an
# operator's laptop held 29-34 GB of BOSS images, five per upgrade. A full disk takes Postgres
# down, which the operator sees as "random issues". This runs after every upgrade and update
# (upgrade.sh, boss.sh, remote.sh) and on demand (`boss prune`).
#
# What it removes:
#   * stopped preview containers (`boss-prev-*`) and preview images no container uses;
#   * dangling images of the boss-community compose project;
#   * boss-community-* image tags other than the current one (<tag>, from .env) and the
#     prev-<tag> rollback point upgrade.sh keeps — never one a container uses;
#   * build cache above BOSS_BUILD_CACHE_CAP_GB (10; 0 turns the cap off). Docker keeps the
#     newest cache up to the cap and drops the rest. The cache is Docker-wide, so on a shared
#     machine the cap also bounds other projects' cache: set it higher or to 0 there;
#   * additionally, on a dedicated BOSS host (--dedicated / BOSS_DEDICATED_HOST=1) above
#     --threshold (75%) disk: build cache older than 72h, down to 20% of the disk;
#   * on a small Docker disk (less than BOSS_DISK_FLOOR_GB, 20, free): build cache down to what
#     fits above that floor, none on a small box (BOSSAPP-1447). Only on a dedicated host or with
#     --room (the installer, right after its own build): the cache is Docker-wide.
# Never volumes, never `system prune`, never a container or image without a BOSS name/label.
set -euo pipefail

DEDICATED=0
ROOM=0
DRY=0
THRESHOLD=75
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
INSTALL_DIR="${BOSS_INSTALL_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd -P)}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dedicated) DEDICATED=1; shift ;;
    --room) ROOM=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --threshold) THRESHOLD="${2:?}"; shift 2 ;;
    --dir) INSTALL_DIR="${2:?--dir needs a path}"; shift 2 ;;
    *) printf 'prune: unknown option %s\n' "$1" >&2; exit 2 ;;
  esac
done
# A setting comes from the environment, else from the install's .env, else the default.
env_or_dotenv() {
  local value="${!1:-}"
  [[ -n "$value" ]] || value="$(grep -m 1 "^$1=" "$INSTALL_DIR/.env" 2>/dev/null | cut -d= -f2- || true)"
  value="${value#"${value%%[![:space:]]*}"}"; value="${value%"${value##*[![:space:]]}"}"
  value="${value#[\"\']}"; value="${value%[\"\']}"
  printf '%s\n' "${value:-$2}"
}
[[ "$(env_or_dotenv BOSS_DEDICATED_HOST 0)" =~ ^(1|true|yes)$ ]] && DEDICATED=1
# The image tag install.sh builds under: BOSS_COMMUNITY_IMAGE_TAG, else the compose project name.
TAG="$(env_or_dotenv BOSS_COMMUNITY_IMAGE_TAG "$(env_or_dotenv COMPOSE_PROJECT_NAME boss-community)")"
CACHE_CAP_GB="$(env_or_dotenv BOSS_BUILD_CACHE_CAP_GB 10)"
[[ "$CACHE_CAP_GB" =~ ^[0-9]+$ ]] || { printf 'prune: BOSS_BUILD_CACHE_CAP_GB must be a whole number of GB (got %s)\n' "$CACHE_CAP_GB" >&2; exit 2; }

if docker info >/dev/null 2>&1; then D=(docker)
elif command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then D=(sudo -n docker)
else printf 'prune: Docker is unavailable to this user\n' >&2; exit 1; fi

run() { if [[ "$DRY" -eq 1 ]]; then printf '  would run: %s\n' "$*"; else "$@" >/dev/null 2>&1 || true; fi; }
used_pct() { df -P / | awk 'NR==2 {gsub("%","",$5); print $5}'; }

before=$(used_pct)
printf 'disk before: %s%% used\n' "$before"

# 1. Stopped preview containers, then preview images nothing uses.
for c in $("${D[@]}" ps -a --filter status=exited --filter status=created --filter status=dead \
             --format '{{.Names}}' | grep '^boss-prev-' || true); do
  run "${D[@]}" rm -f "$c"
done
in_use=$("${D[@]}" ps -a --format '{{.Image}}' | sort -u)
for img in $("${D[@]}" images --format '{{.Repository}}:{{.Tag}}' | grep '^boss-prev-' || true); do
  repo=${img%%:*}
  grep -qxF -e "$img" -e "$repo" <<<"$in_use" || run "${D[@]}" rmi -f "$img"
done

# 2. Dangling images of this compose project.
run "${D[@]}" image prune -f --filter label=com.docker.compose.project=boss-community

# 3. Old BOSS image tags: keep the current release (<tag>), the rollback point (prev-<tag>,
#    BOSSAPP-839) and any tag a container uses; every other tag of a RELEASE-TAGGED repository
#    goes. A repository with no <tag> image is not one the installer tags per release — the
#    database image (boss-community-db:pg16.12-…, built locally, not pullable) is pinned by
#    version — and is never touched, whether or not a container happens to be running.
for repo in $("${D[@]}" images --format '{{.Repository}}' | grep '^boss-community-' | sort -u || true); do
  [[ "$repo" == boss-community-db ]] && continue
  tags="$("${D[@]}" images "$repo" --format '{{.CreatedAt}}|{{.Repository}}:{{.Tag}}' | sort -r | cut -d'|' -f2)"
  grep -qxF -e "$repo:$TAG" -e "$repo:prev-$TAG" <<<"$tags" || continue
  while read -r img; do
    [[ -z "$img" || "$img" == *":<none>" ]] && continue
    [[ "$img" == "$repo:$TAG" || "$img" == "$repo:prev-$TAG" ]] && continue
    [[ "$img" == *":prev-"* ]] && continue
    grep -qxF "$img" <<<"$in_use" || run "${D[@]}" rmi "$img"
  done <<<"$tags"
done

# 4. Build cache. The cap holds on every host: Docker keeps the newest BOSS_BUILD_CACHE_CAP_GB
#    of cache and drops the rest, so the cache can no longer grow without bound between deploys.
if [[ "$CACHE_CAP_GB" -gt 0 ]]; then
  run "${D[@]}" builder prune -f --keep-storage "${CACHE_CAP_GB}gb"
else
  printf 'build cache cap off (BOSS_BUILD_CACHE_CAP_GB=0)\n'
fi
#    Above the threshold, a host that is BOSS's alone also drops cache older than 72h down to
#    20% of the disk; a shared machine keeps other projects' cache.
now=$(used_pct)
if [[ "$now" -ge "$THRESHOLD" ]]; then
  if [[ "$DEDICATED" -eq 1 ]]; then
    total_gb=$(df -BG -P / 2>/dev/null | awk 'NR==2 {gsub("G","",$2); print $2}' || echo 0)
    keep=$(( total_gb / 5 )); [[ "$keep" -lt 1 ]] && keep=1
    run "${D[@]}" builder prune -f --filter until=72h --keep-storage "${keep}gb"
  else
    printf 'disk %s%% used; older build cache left alone beyond the cap (not a dedicated BOSS host). On a machine that\n' "$now"
    printf 'runs only BOSS: %s --dedicated\n' "$0"
  fi
fi

# 5. ROOM TO WORK (BOSSAPP-1447). The cap is a size, and on a small disk a capped cache still
#    leaves too little room: a fresh install on a 30 GB box kept ~4 GB of build cache and left
#    5 GB free, less than a build or the e2e gate needs. When Docker's own disk has less than
#    BOSS_DISK_FLOOR_GB (20) free, keep only the cache that fits above that floor (on a small box,
#    none). Docker's disk is read where it can be: a local Linux daemon's root directory, else
#    from inside the current API image (Docker Desktop, Colima). Unknown: nothing more is pruned.
FLOOR_GB="$(env_or_dotenv BOSS_DISK_FLOOR_GB 20)"
[[ "$FLOOR_GB" =~ ^[0-9]+$ ]] || FLOOR_GB=20
docker_disk_free_gb() {
  local root os kb=""
  IFS='|' read -r root os <<< "$("${D[@]}" info --format '{{.DockerRootDir}}|{{.OperatingSystem}}' 2>/dev/null || true)"
  local endpoint
  endpoint="$("${D[@]}" context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)"
  [[ -z "${DOCKER_HOST:-}" ]] || endpoint="$DOCKER_HOST"
  if [[ "$(uname -s)" == Linux && "${endpoint:-unix://}" == unix://* && "$root" == /* && -d "$root" && "$os" != *"Docker Desktop"* ]]; then
    kb="$(df -Pk "$root" 2>/dev/null | awk 'NR==2 {print $4}')" || kb=""
  elif [[ -n "$root" && "$DRY" -eq 0 ]]; then
    # A container is started to read the VM's disk: never in a dry run, which changes nothing.
    kb="$("${D[@]}" run --rm --pull never --entrypoint df "boss-community-api:$TAG" -Pk / 2>/dev/null </dev/null | awk 'NR==2 {print $4}')" || kb=""
  fi
  [[ "$kb" =~ ^[0-9]+$ ]] && printf '%s\n' $(( kb / 1048576 ))
  return 0
}
build_cache_gb() {  # whole GB, rounded down; "" when unknown
  local size
  size="$("${D[@]}" system df --format '{{.Type}}|{{.Size}}' 2>/dev/null | awk -F'|' '$1 ~ /Build Cache/ {print $2; exit}')" || true
  [[ -n "$size" ]] || return 0
  LC_ALL=C awk -v s="$size" 'BEGIN {
    n = s; gsub(/[^0-9.]/, "", n); u = s; gsub(/[0-9. ]/, "", u); u = tolower(u)
    if (n == "") exit
    if (u == "b") n /= 1e9; else if (u == "kb") n /= 1e6; else if (u == "mb") n /= 1e3
    else if (u == "tb") n *= 1e3
    printf "%d\n", n }'
}
# The cache is Docker-wide: only a host that runs BOSS alone (--dedicated) or the installer right
# after its own build (--room) trims below the cap.
if [[ "$CACHE_CAP_GB" -gt 0 && ( "$DEDICATED" -eq 1 || "$ROOM" -eq 1 ) ]]; then
  free_gb="$(docker_disk_free_gb)"
  if [[ -z "$free_gb" ]]; then
    printf "Docker's disk could not be measured; the build cache is kept at its cap (%s GB)\n" "$CACHE_CAP_GB"
  elif [[ "$free_gb" -lt "$FLOOR_GB" ]]; then
    cache_gb="$(build_cache_gb)"; cache_gb="${cache_gb:-0}"
    keep=$(( free_gb + cache_gb - FLOOR_GB ))
    [[ "$keep" -gt 0 ]] || keep=0
    [[ "$keep" -le "$CACHE_CAP_GB" ]] || keep="$CACHE_CAP_GB"
    printf "Docker's disk has %s GB free, under the %s GB floor (BOSS_DISK_FLOOR_GB): keeping %s GB of build cache\n" \
      "$free_gb" "$FLOOR_GB" "$keep"
    if [[ "$keep" -eq 0 ]]; then
      run "${D[@]}" builder prune -af
    else
      run "${D[@]}" builder prune -af --keep-storage "${keep}gb"
    fi
  fi
fi
printf 'disk after: %s%% used\n' "$(used_pct)"
