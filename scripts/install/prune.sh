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
#   * the prev-<tag> rollback point itself once it is past its window: BOSS_ROLLBACK_KEEP_HOURS
#     (72) after the upgrade that kept it, and only while the running release is healthy
#     (BOSSAPP-1459). .boss-rollback then records BOSS_PREV_EXPIRED_AT and `boss rollback` says
#     the images are gone instead of trying;
#   * build cache above BOSS_BUILD_CACHE_CAP_GB (10; 0 turns the cap off). Docker keeps the
#     newest cache up to the cap and drops the rest. The cache is Docker-wide, so on a shared
#     machine the cap also bounds other projects' cache: set it higher or to 0 there;
#   * additionally, on a dedicated BOSS host (--dedicated / BOSS_DEDICATED_HOST=1) above
#     --threshold (75%) disk: build cache older than 72h, down to 20% of the disk;
#   * on a small Docker disk (less than BOSS_DISK_FLOOR_GB, 20, free): build cache down to what
#     fits above that floor, none on a small box (BOSSAPP-1447). Only on a dedicated host or with
#     --room (the installer, right after its own build): the cache is Docker-wide.
#   * --cache-only: the build cache steps alone (4 and 5; no image, container or rollback point is
#     touched mid-build), for the installer BETWEEN two image families (BOSSAPP-1458).
# Never volumes, never `system prune`, never a container or image without a BOSS name/label.
set -euo pipefail

DEDICATED=0
ROOM=0
CACHE_ONLY=0
DRY=0
THRESHOLD=75
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
INSTALL_DIR="${BOSS_INSTALL_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd -P)}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dedicated) DEDICATED=1; shift ;;
    --room) ROOM=1; shift ;;
    --cache-only) CACHE_ONLY=1; shift ;;
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

failed=0
run() {
  if [[ "$DRY" -eq 1 ]]; then
    printf '  would run: %s\n' "$*"
  elif ! "$@" >/dev/null; then
    failed=1
    printf 'prune: could not complete %s\n' "$*" >&2
  fi
}
used_pct() { df -P "$INSTALL_DIR" | awk 'NR==2 {gsub("%","",$5); print $5}'; }

before=$(used_pct)
printf 'disk before: %s%% used\n' "$before"

if [[ "$CACHE_ONLY" -eq 0 ]]; then
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
fi

# 3b. THE ROLLBACK POINT HAS A WINDOW (BOSSAPP-1459). upgrade.sh keeps the images that ran before
#     it as prev-<tag> so `boss rollback` takes seconds; kept for ever they were a whole second
#     image set on every install (~15 GB real on the operator's laptop). Once the upgrade has run
#     BOSS_ROLLBACK_KEEP_HOURS (72) and the API is healthy, the release is known good and the
#     rollback images go. The record stays, marked, so `boss rollback` says what happened. An
#     unhealthy release keeps them: that is exactly when a rollback is needed.
PROJECT="$(env_or_dotenv COMPOSE_PROJECT_NAME boss-community)"
ROLLBACK_FILE="$INSTALL_DIR/.boss-rollback"
KEEP_HOURS="$(env_or_dotenv BOSS_ROLLBACK_KEEP_HOURS 72)"
[[ "$KEEP_HOURS" =~ ^[0-9]+$ && "$KEEP_HOURS" -ge 1 ]] || KEEP_HOURS=72
rb_value() { grep -m 1 "^$1=" "$ROLLBACK_FILE" 2>/dev/null | cut -d= -f2- || true; }
hours_since() {  # whole hours since an ISO UTC stamp; empty when it cannot be read
  python3 - "$1" <<'PY' 2>/dev/null || true
import sys
from datetime import datetime, timezone
try:
    t = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
except ValueError:
    sys.exit(0)
t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
print(int((datetime.now(timezone.utc) - t).total_seconds() // 3600))
PY
}
api_healthy() {
  [[ -n "$("${D[@]}" ps -q --filter "label=com.docker.compose.project=$PROJECT" \
            --filter "label=com.docker.compose.service=api" --filter health=healthy 2>/dev/null)" ]]
}
if [[ "$CACHE_ONLY" -eq 0 && -f "$ROLLBACK_FILE" && -z "$(rb_value BOSS_PREV_EXPIRED_AT)" && -n "$(rb_value BOSS_PREV_SAVED_AT)" ]]; then
  age_h="$(hours_since "$(rb_value BOSS_PREV_SAVED_AT)")"
  prev_tag="$(rb_value BOSS_PREV_IMAGE_TAG)"
  if [[ "$age_h" =~ ^[0-9]+$ && "$age_h" -ge "$KEEP_HOURS" && "$prev_tag" == "prev-$TAG" ]]; then
    if api_healthy; then
      dropped=0
      for repo in $(rb_value BOSS_PREV_IMAGES); do
        [[ "$repo" =~ ^boss-community-[a-z0-9-]+$ ]] || continue
        img="$repo:$prev_tag"
        "${D[@]}" image inspect "$img" >/dev/null 2>&1 || continue
        if grep -qxF "$img" <<<"$in_use"; then
          printf 'kept %s: a container still runs it\n' "$img"
          continue
        fi
        run "${D[@]}" rmi "$img"
        dropped=$((dropped + 1))
      done
      # Marked only when every kept image is really gone (an rmi can fail, a container can still
      # run one): otherwise `boss rollback` would refuse while its images are still here.
      left=0
      for repo in $(rb_value BOSS_PREV_IMAGES); do
        [[ "$repo" =~ ^boss-community-[a-z0-9-]+$ ]] || continue
        "${D[@]}" image inspect "$repo:$prev_tag" >/dev/null 2>&1 && left=$((left + 1))
      done
      if [[ "$DRY" -eq 0 && "$left" -eq 0 ]]; then
        printf 'BOSS_PREV_EXPIRED_AT=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$ROLLBACK_FILE"
      fi
      printf '%s the rollback images of %s (%s images, kept %s h; the window is %s h)\n' \
        "$([[ "$DRY" -eq 1 ]] && echo 'would drop' || echo dropped)" \
        "$(rb_value BOSS_PREV_REF | cut -c1-40)" "$dropped" "$age_h" "$KEEP_HOURS"
    else
      printf 'kept the rollback images: the running release is not healthy, so `boss rollback` may be needed\n'
    fi
  fi
fi

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
  # A Colima or Desktop VM grows a sparse disk on the Mac. It can still report
  # free blocks when the backing filesystem is already below the build floor.
  context="${DOCKER_CONTEXT:-}"
  if [[ -z "$context" ]]; then context="$("${D[@]}" context show 2>/dev/null || true)"; fi
  case "$(uname -s):$context" in
    Darwin:colima|Darwin:colima-*|Darwin:desktop-linux)
      host_kb="$(df -Pk "$INSTALL_DIR" 2>/dev/null | awk 'NR==2 {print $4}')" || host_kb=""
      if [[ -z "${DOCKER_HOST:-}" && "$host_kb" =~ ^[0-9]+$ && "$free_gb" =~ ^[0-9]+$ ]] &&
         (( host_kb / 1048576 < FLOOR_GB && host_kb / 1048576 < free_gb )); then
        printf "The Mac backing this install has %s GiB free, under the %s GiB floor. Releasing unused build cache.\n" "$((host_kb / 1048576))" "$FLOOR_GB"
        run "${D[@]}" builder prune -af
        # The VM disk may still need trimming before the Mac recovers the freed blocks.
        free_gb="$(docker_disk_free_gb)"
      fi ;;
  esac
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
exit "$failed"
