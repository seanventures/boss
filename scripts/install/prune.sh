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
#   * build cache above BOSS_BUILD_CACHE_CAP_GB (10; 0 turns the cap off), as far as Docker's
#     plain capped prune goes. The cache is Docker-wide, so on a shared machine the cap also
#     bounds other projects' cache: set it higher or to 0 there;
#   * in BOSS's own scope only — a Docker that runs only BOSS (--dedicated /
#     BOSS_DEDICATED_HOST=1) or BOSS's buildx builder (BOSS_BUILDX_BUILDER) — also: the rest of
#     the unused cache down to the cap (--all); above --threshold (75%) disk, cache older than 72h
#     down to 20% of the disk; on a small Docker disk (less than BOSS_DISK_FLOOR_GB, 20, free) or
#     a tight Mac under a Colima/Desktop VM, the cache down to what fits above that floor
#     (BOSSAPP-1447). On a shared Docker those are printed with the exact command and left to the
#     person: its cache may be their own projects'.
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
#    The cap counts what Docker can free (Reclaimable): the rest is layers that installed images
#    (the release, its rollback set) still use, which no prune frees. The before and after are
#    Docker's own readings, printed below (ENT-DISK-02: an upgrade said it pruned and left 17.2 GB).
#
#    THE CACHE BOSS MAY CLEAR is the steward's scope (verb_prune_build_cache): BOSS's own buildx
#    builder (BOSS_BUILDX_BUILDER), or a Docker that runs only BOSS (BOSS_DEDICATED_HOST=1 /
#    --dedicated). Only there does BOSS go further than the plain capped prune — --all, the
#    floor trims, a keep-less prune. On any other Docker the rest of the cache may be the
#    person's own projects': BOSS prints the reading and the command, and leaves it to them.
#    A Colima or Desktop VM is not proof of either: BOSS does not create the profile.
BUILDER="$(env_or_dotenv BOSS_BUILDX_BUILDER "")"
if [[ -n "$BUILDER" ]]; then
  CACHE_PRUNE=("${D[@]}" buildx prune --builder "$BUILDER")
  OWNS_CACHE=1
else
  CACHE_PRUNE=("${D[@]}" builder prune)
  OWNS_CACHE="$DEDICATED"
fi
LEFT_TO_YOU=()   # what BOSS would have cleared on its own Docker, and the command for it
leave() { LEFT_TO_YOU+=("$1|$2"); }
# "<size GB>|<freeable GB>" of the cache in that scope ("<size>|" when Docker does not say what
# it can free); "" when Docker says nothing.
GB_AWK='function gb(s,  n, u) {
      split(s, w, " "); n = w[1]; u = w[1]; gsub(/[^0-9.]/, "", n); gsub(/[0-9.]/, "", u); u = tolower(u)
      if (n == "") return -1
      if (u == "b") return n / 1e9; if (u == "kb") return n / 1e6; if (u == "mb") return n / 1e3
      if (u == "tb") return n * 1e3; return n + 0 }'
cache_reading() {
  if [[ -n "$BUILDER" ]]; then
    "${D[@]}" buildx du --builder "$BUILDER" 2>/dev/null | LC_ALL=C awk "$GB_AWK"'
      BEGIN { s = -1; p = -1 }
      # `buildx du` counts layers images share as Reclaimable too; what a prune can free is Private
      # (it equals `docker system df` Reclaimable on the same daemon).
      $1 == "Private:" { p = gb($2) }
      $1 == "Total:" { s = gb($2) }
      END { if (s >= 0 && p >= 0) printf "%.1f|%.1f\n", s, (p < s ? p : s); else if (s >= 0) printf "%.1f|\n", s }' || true
  else
    "${D[@]}" system df --format '{{.Type}}|{{.Size}}|{{.Reclaimable}}' 2>/dev/null | LC_ALL=C awk -F'|' "$GB_AWK"'
      $1 ~ /Build Cache/ { s = gb($2); r = gb($3)
                           if (s >= 0 && r < 0) printf "%.1f|\n", s   # an older Docker: no Reclaimable
                           else if (s >= 0) printf "%.1f|%.1f\n", s, (r < s ? r : s); exit }' || true
  fi
}
cache_before=""
if [[ "$CACHE_CAP_GB" -gt 0 ]]; then
  cache_before="$(cache_reading)"
  run "${CACHE_PRUNE[@]}" -f --keep-storage "${CACHE_CAP_GB}gb"
  # A plain prune passes over some unused cache: what can be freed may still be over the cap.
  freeable="$(cache_reading)"; freeable="${freeable#*|}"
  if [[ -n "$freeable" ]] && awk -v f="$freeable" -v c="$CACHE_CAP_GB" 'BEGIN { exit !(f + 0 > c + 0) }'; then
    if [[ "$OWNS_CACHE" -eq 1 ]]; then
      run "${CACHE_PRUNE[@]}" -af --keep-storage "${CACHE_CAP_GB}gb"
    else
      leave "${freeable} GB of build cache Docker can free is over the ${CACHE_CAP_GB} GB cap" \
            "docker builder prune -af --keep-storage ${CACHE_CAP_GB}gb"
    fi
  fi
else
  printf 'build cache cap off (BOSS_BUILD_CACHE_CAP_GB=0)\n'
fi
#    Above the threshold, a host that is BOSS's alone also drops cache older than 72h down to
#    20% of the disk; a shared machine keeps other projects' cache.
now=$(used_pct)
if [[ "$now" -ge "$THRESHOLD" ]]; then
  if [[ "$OWNS_CACHE" -eq 1 ]]; then
    total_gb=$(df -BG -P / 2>/dev/null | awk 'NR==2 {gsub("G","",$2); print $2}' || echo 0)
    keep=$(( total_gb / 5 )); [[ "$keep" -lt 1 ]] && keep=1
    run "${CACHE_PRUNE[@]}" -f --filter until=72h --keep-storage "${keep}gb"
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
#    Only in BOSS's own scope (above); elsewhere the person gets the reading and the command.
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
build_cache_gb() {  # whole GB of the cache in BOSS's scope, rounded down; "" when unknown
  local reading
  reading="$(cache_reading)"
  [[ -n "$reading" ]] || return 0
  printf '%s\n' "${reading%%[.|]*}"
}
# Only a host that runs BOSS alone (--dedicated), BOSS's own builder, or the installer right
# after its own build (--room) looks at the floor; only BOSS's scope is trimmed below the cap.
if [[ "$CACHE_CAP_GB" -gt 0 && ( "$OWNS_CACHE" -eq 1 || "$ROOM" -eq 1 ) ]]; then
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
        if [[ "$OWNS_CACHE" -eq 1 ]]; then
          printf "The Mac backing this install has %s GiB free, under the %s GiB floor. Releasing unused build cache.\n" "$((host_kb / 1048576))" "$FLOOR_GB"
          run "${CACHE_PRUNE[@]}" -af
          # The VM disk may still need trimming before the Mac recovers the freed blocks.
          free_gb="$(docker_disk_free_gb)"
        else
          printf "The Mac backing this install has %s GiB free, under the %s GiB floor.\n" "$((host_kb / 1048576))" "$FLOOR_GB"
          leave "the Mac backing this install has $((host_kb / 1048576)) GiB free, under the ${FLOOR_GB} GiB floor" \
                "docker builder prune -af"
        fi
      fi ;;
  esac
  if [[ -z "$free_gb" ]]; then
    printf "Docker's disk could not be measured; the build cache is kept at its cap (%s GB)\n" "$CACHE_CAP_GB"
  elif [[ "$free_gb" -lt "$FLOOR_GB" ]]; then
    cache_gb="$(build_cache_gb)"; cache_gb="${cache_gb:-0}"
    keep=$(( free_gb + cache_gb - FLOOR_GB ))
    [[ "$keep" -gt 0 ]] || keep=0
    [[ "$keep" -le "$CACHE_CAP_GB" ]] || keep="$CACHE_CAP_GB"
    keep_args=(); [[ "$keep" -eq 0 ]] || keep_args=(--keep-storage "${keep}gb")
    if [[ "$OWNS_CACHE" -eq 1 ]]; then
      printf "Docker's disk has %s GB free, under the %s GB floor (BOSS_DISK_FLOOR_GB): keeping %s GB of build cache\n" \
        "$free_gb" "$FLOOR_GB" "$keep"
      run "${CACHE_PRUNE[@]}" -af ${keep_args[@]+"${keep_args[@]}"}
    else
      printf "Docker's disk has %s GB free, under the %s GB floor (BOSS_DISK_FLOOR_GB)\n" "$free_gb" "$FLOOR_GB"
      leave "Docker's disk has ${free_gb} GB free, under the ${FLOOR_GB} GB floor; ${keep} GB of build cache fits above it" \
            "docker builder prune -af${keep_args[@]+ ${keep_args[*]}}"
    fi
  fi
fi
# What the build cache really is now, from Docker, not what the steps above meant to do.
if [[ -n "$cache_before" ]]; then
  cache_after="$cache_before"
  [[ "$DRY" -eq 1 ]] || cache_after="$(cache_reading)"
  if [[ -n "$cache_after" ]]; then
    IFS='|' read -r size_b free_b <<<"$cache_before"
    IFS='|' read -r size_a free_a <<<"$cache_after"
    if [[ -z "$free_a" ]]; then
      printf 'build cache%s: %s GB before, %s GB after; the cap is %s GB (this Docker does not say how much it can free)\n' \
        "${BUILDER:+ (builder $BUILDER)}" "$size_b" "$size_a" "$CACHE_CAP_GB"
    else
      printf 'build cache%s: %s GB before (%s GB Docker can free), %s GB after (%s GB Docker can free); the cap is %s GB\n' \
        "${BUILDER:+ (builder $BUILDER)}" "$size_b" "${free_b:-?}" "$size_a" "$free_a" "$CACHE_CAP_GB"
      shared="$(awk -v s="$size_a" -v f="$free_a" 'BEGIN { printf "%.1f", s - f }')"
      if awk -v x="$shared" 'BEGIN { exit !(x + 0 >= 0.1) }'; then
        printf '  %s GB of it is layers installed images still use: it goes when they go, and no prune frees it\n' "$shared"
      fi
    fi
    if [[ -n "$free_a" && "$DRY" -eq 0 && "$OWNS_CACHE" -eq 1 ]] && awk -v f="$free_a" -v c="$CACHE_CAP_GB" 'BEGIN { exit !(f + 0 > c + 0) }'; then
      failed=1
      printf 'prune: the build cache Docker can free is still %s GB, over the %s GB cap\n' "$free_a" "$CACHE_CAP_GB" >&2
    fi
  fi
fi
# On a shared Docker: what BOSS left, why, and the exact command — the person decides.
if [[ "${#LEFT_TO_YOU[@]}" -gt 0 ]]; then
  printf 'build cache left to you: this Docker is shared (no BOSS_DEDICATED_HOST=1, no BOSS_BUILDX_BUILDER), so its cache may be other projects'"'"' and BOSS does not clear it.\n'
  for item in "${LEFT_TO_YOU[@]}"; do
    printf '  %s. To free it yourself: %s\n' "${item%%|*}" "${item#*|}"
  done
  printf '  On a machine that runs only BOSS, set BOSS_DEDICATED_HOST=1 in .env and BOSS does this itself.\n'
fi
printf 'disk after: %s%% used\n' "$(used_pct)"
exit "$failed"
