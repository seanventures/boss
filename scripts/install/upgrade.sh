#!/usr/bin/env bash
# Back up a running Community install, then move it forward along the ref it follows.
set -euo pipefail
umask 077

# This script also runs piped (bash -s) against older installs, so it carries its own copy of
# the release rules. The awk program between the markers is identical in install.sh and
# scripts/install/bootstrap.sh; a test keeps the copies equal.
#
# WHY A RECORD. The installer used to default to main and forget which ref it installed, so a
# rerun without --ref fast-forwarded a release install to main. install.sh now writes the
# installed ref to .boss-release (not secret), and this script reads it.

# stdin: tag names. $1: 1 to consider prereleases. stdout: the newest tag or nothing.
# A final release outranks its own prereleases; prerelease labels compare as text.
pick_release() {
  # >>> boss_pick_release
  LC_ALL=C awk -v pre="${1:-0}" '
    /^v[0-9]+(\.[0-9]+)*(-[0-9A-Za-z.]+)?$/ {
      core = substr($0, 2); label = ""
      cut = index(core, "-")
      if (cut > 0) { label = substr(core, cut + 1); core = substr(core, 1, cut - 1) }
      if (label != "" && pre != 1) next
      n = split(core, part, ".")
      if (n > 3) next
      # Numbers inside the label compare as numbers (rc10.4 after rc9, rc10.10 after rc10.9).
      key = ""
      while (match(label, /[0-9]+/)) {
        key = key substr(label, 1, RSTART - 1) sprintf("%09d", substr(label, RSTART, RLENGTH))
        label = substr(label, RSTART + RLENGTH)
      }
      label = key label
      printf "%09d.%09d.%09d.%s\t%s\n", part[1], (n >= 2 ? part[2] : 0), (n >= 3 ? part[3] : 0), (label == "" ? "~" : label), $0
    }' | LC_ALL=C sort | tail -n 1 | cut -f 2
  # <<< boss_pick_release
}

ref_is_valid() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ && "$1" != *..* && "$1" != *.lock ]]
}

# release, pinned, or branch.
ref_channel() {
  if [[ "$1" =~ ^v[0-9]+(\.[0-9]+)*(-[0-9A-Za-z.]+)?$ ]]; then
    echo release
  elif [[ "$1" =~ ^[0-9a-fA-F]{40}$ ]]; then
    echo pinned
  else
    echo branch
  fi
}

# The ref an install follows: the recorded one, else what an older install implies. A release
# tag at HEAD stays on releases; a detached commit stays pinned; a branch checkout follows
# main, as older installers did.
installed_ref() {
  local dir="$1" ref tag
  ref="$(grep -m 1 '^BOSS_RELEASE_REF=' "$dir/.boss-release" 2>/dev/null | cut -d= -f2- || true)"
  if [[ -n "$ref" ]] && ref_is_valid "$ref"; then
    echo "$ref"
    return 0
  fi
  tag="$(git -C "$dir" tag --points-at HEAD 2>/dev/null | pick_release 1 || true)"
  if [[ -n "$tag" ]]; then
    echo "$tag"
  elif ! git -C "$dir" symbolic-ref -q HEAD >/dev/null 2>&1 &&
       git -C "$dir" rev-parse --verify -q HEAD >/dev/null 2>&1; then
    git -C "$dir" rev-parse HEAD
  else
    echo main
  fi
}

# RELEASE CHANNELS (BOSSAPP-895). BOSS_CHANNEL in .boss-release says which releases a release
# install follows: `stable` only final releases (v1.2), `rc` also release candidates (v1.3-rc1),
# for early adopters. Unset keeps the rule installs always had: a stable install stays on stable
# releases, a prerelease install may also move to a newer prerelease. Never a downgrade.
channel_is_valid() { [[ "$1" == stable || "$1" == rc ]]; }
recorded_channel() {
  grep -m 1 '^BOSS_CHANNEL=' "$1/.boss-release" 2>/dev/null | cut -d= -f2- || true
}
# Record the channel in .boss-release (the installer keeps the line on every rerun).
record_channel() {
  local dir="$1" channel="$2" tmp
  tmp="$dir/.boss-release.writing.$$"
  { grep -v '^BOSS_CHANNEL=' "$dir/.boss-release" 2>/dev/null || true
    printf 'BOSS_CHANNEL=%s\n' "$channel"; } > "$tmp"
  mv -f "$tmp" "$dir/.boss-release"
}

INSTALL_DIR="${BOSS_INSTALL_DIR:-$HOME/boss}"
REF="${BOSS_REPO_REF:-}"
CHANNEL=""
BACKUP_DIR=""
WEB_MODE=""
DRY_RUN=0
ROLLBACK=1
DISK_CHECK=1
WITHOUT=()

usage() {
  cat <<'EOF'
Usage: upgrade.sh [--dir INSTALL_DIR] [--ref REF] [--channel stable|rc] [--backup-dir PATH]
                  [--web | --no-web] [--dry-run] [--no-rollback] [--without SFX]...
                  [--skip-disk-check]

Without --ref, an install stays on the channel it was installed from:
  release  moves to the newest published release tag (v*), never to main
           --channel stable: final releases only; --channel rc: release candidates too
           (recorded as BOSS_CHANNEL in .boss-release, so later upgrades follow it)
  branch   fast-forwards that branch (main for installs made before releases were recorded)
  pinned   a commit install needs --ref to move
The installed ref is recorded in .boss-release; `boss upgrade` runs this script.

Fetch a fast-forward update, back up .env/Postgres/artifacts/local repositories, then run the
fetched installer. Existing credentials and data volumes are retained.
The backup path is printed before any source or container change.
The installer remembers whether Mission Control was enabled. For installs
created before that setting was recorded, pass --no-web once to keep it off.
Use --web or --no-web to change the existing choice deliberately.
If the new release fails to start, the previous release is put back and
started again (data volumes are kept). --no-rollback leaves it as it is.
The images running before the upgrade are kept as prev-<tag>: `boss rollback`
(rollback.sh) puts that release back later, without a build. One previous set only: the next
upgrade replaces it, and once this release has run BOSS_ROLLBACK_KEEP_HOURS (72) with a
healthy API, prune.sh drops it (docs/operations/DISK_BUDGET.md).

DISK CHECK (storage hygiene layer 1). Before anything is built, the free space on this host
and on Docker's own disk is measured against what the upgrade will need: about
BOSS_UPGRADE_FULL_BUILD_GB (35) for a full rebuild when the Docker build cache is cold, about
BOSS_UPGRADE_INCREMENTAL_BUILD_GB (5) when it is warm, plus the BOSS_DISK_FLOOR_GB (20) that
must stay free. Below that the upgrade refuses, in plain words, and nothing changes.
--skip-disk-check (or BOSS_DISK_CHECK=off in .env) goes ahead regardless. After a successful
upgrade, prune.sh removes BOSS image tags other than the new release and its prev-<tag>
rollback point, and caps the build cache (BOSS_BUILD_CACHE_CAP_GB, 10).

LOCAL BOSS SELF-FIXES (docs/product/BOSS_FIXES_ITSELF.md §6.4) live on a separate stack ref
over the release, never on the checkout's HEAD, so the fast-forward rule still applies to the
release itself. Each installed fix is planned against the new release first: dropped when the
release already has it, carried (re-applied after the upgrade), or needing rework when it no
longer applies. A fix that needs rework blocks the upgrade (nothing changes) until BOSS has
re-ported it, or you upgrade without it: --without SFX (its recommendation reopens).
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="${2:?--dir needs a path}"; shift 2 ;;
    --ref) REF="${2:?--ref needs a ref}"; shift 2 ;;
    --channel)
      channel_is_valid "${2:-}" || { echo '--channel takes stable or rc' >&2; exit 2; }
      CHANNEL="$2"; shift 2 ;;
    --backup-dir) BACKUP_DIR="${2:?--backup-dir needs a path}"; shift 2 ;;
    --no-web|--web)
      [[ -z "$WEB_MODE" ]] || { echo 'Choose only one of --web and --no-web.' >&2; exit 2; }
      WEB_MODE="$1"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --no-rollback) ROLLBACK=0; shift ;;
    --skip-disk-check) DISK_CHECK=0; shift ;;
    --without)
      [[ "${2:-}" =~ ^sfx_[0-9a-f]{6,40}$ ]] || { echo '--without needs a self-fix id (sfx_...)' >&2; exit 2; }
      WITHOUT+=("$2"); shift 2 ;;
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

# The one compose-file resolver (compose_files.sh): the engine inner-sandbox overlay when .env
# opts in. The installer this runs recreates the containers through the same resolver.
upgrade_compose_files() {
  BOSS_COMPOSE_FILES=(-f "$INSTALL_DIR/scripts/install/community-compose.yml")
  [[ -f "$INSTALL_DIR/scripts/install/compose_files.sh" ]] || return 0
  # shellcheck source=compose_files.sh
  source "$INSTALL_DIR/scripts/install/compose_files.sh"
  boss_compose_files "$INSTALL_DIR" "$INSTALL_DIR/.env"
}

OLD_COMMIT="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
CURRENT_REF="$(installed_ref "$INSTALL_DIR")"
if [[ -n "$CHANNEL" ]]; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "Would follow the $CHANNEL channel from now on (BOSS_CHANNEL in .boss-release)."
  else
    record_channel "$INSTALL_DIR" "$CHANNEL"
    echo "Following the $CHANNEL channel from now on."
  fi
else
  CHANNEL="$(recorded_channel "$INSTALL_DIR")"
  channel_is_valid "$CHANNEL" || CHANNEL=""
fi
if [[ -z "$REF" ]]; then
  case "$(ref_channel "$CURRENT_REF")" in
    release)
      # The newest release tag, and never an older one: the installed tag is a candidate.
      # A stable install moves only to a stable release; a prerelease install may also move
      # to a newer prerelease. BOSS_CHANNEL overrides which releases count (BOSSAPP-895).
      include_pre=0
      [[ "$CURRENT_REF" != *-* ]] || include_pre=1
      case "$CHANNEL" in stable) include_pre=0 ;; rc) include_pre=1 ;; esac
      tags="$(git -C "$INSTALL_DIR" ls-remote --tags --refs origin 'v*' </dev/null | sed 's#.*refs/tags/##')" ||
        { echo 'Could not list releases from origin; nothing changed.' >&2; exit 1; }
      REF="$(printf '%s\n%s\n' "$tags" "$CURRENT_REF" | pick_release "$include_pre")"
      [[ -n "$REF" ]] || REF="$CURRENT_REF"
      # NEVER A DOWNGRADE: a prerelease install switched to `stable` waits for a final release
      # newer than the candidate it runs, rather than stepping back to an older one.
      if [[ "$REF" != "$CURRENT_REF" &&
            "$(printf '%s\n%s\n' "$REF" "$CURRENT_REF" | pick_release 1)" != "$REF" ]]; then
        echo "No ${CHANNEL:-stable} release is newer than $CURRENT_REF yet; staying on it."
        REF="$CURRENT_REF"
      fi
      if [[ "$REF" == "$CURRENT_REF" ]]; then
        echo "Already on the newest release, $CURRENT_REF; verifying with a safe installer rerun."
      else
        echo "Newest release: $REF (installed: $CURRENT_REF)."
      fi ;;
    pinned)
      echo "This install is pinned to commit $CURRENT_REF. Pass --ref (boss upgrade --to REF) to move it; nothing changed." >&2
      exit 1 ;;
    *)
      [[ -z "$CHANNEL" ]] ||
        echo "BOSS_CHANNEL=$CHANNEL applies to release installs; this one follows $CURRENT_REF."
      REF="$CURRENT_REF" ;;
  esac
fi
ref_is_valid "$REF" || { echo 'That ref is not a valid Git ref name; nothing changed.' >&2; exit 2; }

git -C "$INSTALL_DIR" fetch origin "$REF" </dev/null
NEW_COMMIT="$(git -C "$INSTALL_DIR" rev-parse 'FETCH_HEAD^{commit}')"
git -C "$INSTALL_DIR" merge-base --is-ancestor "$OLD_COMMIT" "$NEW_COMMIT" ||
  { echo 'Upstream cannot fast-forward this checkout; nothing changed.' >&2; exit 1; }
git -C "$INSTALL_DIR" cat-file -e "$NEW_COMMIT:scripts/install/backup.sh" 2>/dev/null ||
  { echo 'Target ref has no backup helper; source and containers were not changed.' >&2; exit 1; }
[[ "$OLD_COMMIT" != "$NEW_COMMIT" ]] || echo 'Already at the requested source commit; verifying with a safe installer rerun.'

# LOCAL SELF-FIXES: plan each against the new release before anything changes (§6.4).
STACK_REF="$(grep -m 1 '^BOSS_STACK_REF=' "$INSTALL_DIR/.boss-release" 2>/dev/null | cut -d= -f2- || true)"
SELF_FIX="$INSTALL_DIR/scripts/install/self_fix_installer.py"
without_args=()
for sfx in ${WITHOUT[@]+"${WITHOUT[@]}"}; do without_args+=(--without "$sfx"); done
if [[ -n "$STACK_REF" && -f "$SELF_FIX" ]]; then
  command -v python3 >/dev/null 2>&1 ||
    { echo 'This install carries local BOSS self-fixes and python3 is needed to plan them; nothing changed.' >&2; exit 1; }
  PLAN="$(python3 "$SELF_FIX" --dir "$INSTALL_DIR" plan-upgrade --to "$NEW_COMMIT" --ref "$STACK_REF" --json)" ||
    { echo 'Could not plan the local BOSS self-fixes against the new release; nothing changed.' >&2; exit 1; }
  BLOCKED="$(printf '%s' "$PLAN" | python3 -c '
import json, sys
plan = json.load(sys.stdin)
skip = set(sys.argv[1:])
print(" ".join(f["self_fix_id"] or f["commit"][:12] for f in plan["fixes"]
               if f["outcome"] == "needs_rework" and f["self_fix_id"] not in skip))
' ${WITHOUT[@]+"${WITHOUT[@]}"})"
  printf '%s' "$PLAN" | python3 -c '
import json, sys
for f in json.load(sys.stdin)["fixes"]:
    print("  local fix %s: %s" % (f["self_fix_id"] or f["commit"][:12],
                                  f["outcome"].replace("_", " ")))
'
  if [[ -n "$BLOCKED" ]]; then
    echo "These local BOSS fixes no longer apply to $REF: $BLOCKED" >&2
    echo 'BOSS re-ports them through the normal self-fix loop (with your approval). To upgrade now without one, pass --without SFX; its recommendation reopens. Nothing changed.' >&2
    exit 1
  fi
fi

# INSTANT ROLLBACK (BOSSAPP-839): before anything is rebuilt, the images that run now are kept
# under prev-<tag>, and .boss-rollback records what they need (commit, ref, schema checksum), so
# `boss rollback` can put this release back in seconds, without a build. Best effort: an install
# whose images cannot be tagged still upgrades; it just has no instant way back.
ROLLBACK_SERVICES=(api worker mcp-tools egress web)
image_tag() {
  local tag
  tag="$(grep -m 1 '^BOSS_COMMUNITY_IMAGE_TAG=' "$INSTALL_DIR/.env" 2>/dev/null | cut -d= -f2- || true)"
  [[ -n "$tag" ]] || tag="$(grep -m 1 '^COMPOSE_PROJECT_NAME=' "$INSTALL_DIR/.env" 2>/dev/null | cut -d= -f2- || true)"
  printf '%s\n' "${tag:-boss-community}"
}
sha256_stdin() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1; else shasum -a 256 | cut -d' ' -f1; fi
}
keep_previous_images() {
  local tag="$1" kept=() d=(docker) svc
  if ! docker info >/dev/null 2>&1; then
    if command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then d=(sudo -n docker)
    else echo 'Docker is unavailable to this user; no instant rollback point kept.' >&2; return 0; fi
  fi
  # BOSSAPP-1204: a previous attempt already moved this tag and died before the container
  # ran it. Retagging prev-$tag now would point it at the image the container never ran, and
  # the resume's backup would refuse. Leave the rollback point that still names the running image.
  local tag_id="" run_id="" prev_id="" cid=""
  tag_id="$("${d[@]}" image inspect --format '{{.Id}}' "boss-community-api:$tag" 2>/dev/null || true)"
  upgrade_compose_files || true
  cid="$("${d[@]}" compose "${BOSS_COMPOSE_FILES[@]}" --env-file "$INSTALL_DIR/.env" ps -q api 2>/dev/null || true)"
  [[ -z "$cid" ]] || run_id="$("${d[@]}" inspect --format '{{.Image}}' "$cid" 2>/dev/null || true)"
  prev_id="$("${d[@]}" image inspect --format '{{.Id}}' "boss-community-api:prev-$tag" 2>/dev/null || true)"
  if [[ -n "$run_id" && -n "$tag_id" && -n "$prev_id" && "$run_id" == "$prev_id" && "$run_id" != "$tag_id" && -f "$INSTALL_DIR/.boss-rollback" ]]; then
    echo 'A previous upgrade moved the API tag and stopped before the container ran it. Keeping that rollback point.' >&2
    return 0
  fi
  for svc in "${ROLLBACK_SERVICES[@]}"; do
    if "${d[@]}" image inspect "boss-community-$svc:$tag" >/dev/null 2>&1; then
      "${d[@]}" tag "boss-community-$svc:$tag" "boss-community-$svc:prev-$tag" </dev/null &&
        kept+=("boss-community-$svc")
    fi
  done
  [[ ${#kept[@]} -gt 0 ]] || { echo 'No current BOSS images found; no instant rollback point kept.' >&2; return 0; }
  local schema tmp="$INSTALL_DIR/.boss-rollback.writing.$$"
  schema="$(git -C "$INSTALL_DIR" show "$OLD_COMMIT:db/schema.sql" 2>/dev/null | sha256_stdin)" || schema=""
  printf '%s\n' \
    "# BOSS rollback point, written by upgrade.sh. Not secret. boss rollback reads it." \
    "BOSS_PREV_COMMIT=$OLD_COMMIT" \
    "BOSS_PREV_REF=$CURRENT_REF" \
    "BOSS_IMAGE_TAG=$tag" \
    "BOSS_PREV_IMAGE_TAG=prev-$tag" \
    "BOSS_PREV_IMAGES=${kept[*]}" \
    "BOSS_PREV_SCHEMA_SHA256=$schema" \
    "BOSS_PREV_SAVED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp"
  mv -f "$tmp" "$INSTALL_DIR/.boss-rollback"
  echo "Kept the running images as prev-$tag; boss rollback puts $CURRENT_REF back without a build." >&2
}

# DISK-AWARE ADMISSION (storage hygiene layer 1, BOSSAPP-1150). An upgrade that starts on a
# nearly full disk stops half way with the old images gone and the new ones unfinished (the
# operator's deploy stopped at 7 GiB free, 2026-11-14). So before anything is built: measure
# the free space where this install and its backups live AND on Docker's own disk (on Docker
# Desktop a separate virtual disk; the host can have room while it is full), estimate what the
# build will take, and refuse — in plain words, with nothing changed — when either is short.
env_or_dotenv() {
  local value="${!1:-}"
  [[ -n "$value" ]] || value="$(grep -m 1 "^$1=" "$INSTALL_DIR/.env" 2>/dev/null | cut -d= -f2- || true)"
  value="${value#"${value%%[![:space:]]*}"}"; value="${value%"${value##*[![:space:]]}"}"
  value="${value#[\"\']}"; value="${value%[\"\']}"
  printf '%s\n' "${value:-$2}"
}
# >>> boss_disk_admission
# Pure. $1 host free GB; $2 Docker-disk free GB or "" (unknown); $3 build cache GB or "";
# $4 1 when the current BOSS images exist; $5 floor GB; $6 full-rebuild GB; $7 incremental GB.
# Prints one line: "ok ..." or "refuse ...". Exit 1 on refuse.
boss_disk_verdict() {
  LC_ALL=C awk -v host="$1" -v dock="$2" -v cache="$3" -v images="$4" \
                -v floor="$5" -v full="$6" -v incr="$7" 'BEGIN {
    if (images == 1 && cache != "" && cache + 0 >= 1) {
      need = incr + 0; what = sprintf("an incremental build (about %g GB; the Docker build cache holds %.1f GB)", incr, cache)
    } else if (images == 1) {
      need = full + 0; what = sprintf("a full rebuild (about %g GB; the Docker build cache is cold)", full)
    } else {
      need = full + 0; what = sprintf("a full build (about %g GB; no BOSS images here yet)", full)
    }
    total = need + floor
    fix = "Free space (boss prune; old backups under the backups directory), lower BOSS_DISK_FLOOR_GB, or pass --skip-disk-check. Nothing changed."
    if (host + 0 < total) {
      printf "refuse Not enough disk for this upgrade: this host has %.1f GB free, and %s plus the %g GB floor (BOSS_DISK_FLOOR_GB) needs at least %g GB. %s\n", host, what, floor, total, fix
      exit 1
    }
    if (dock != "" && dock + 0 < total) {
      printf "refuse Not enough disk for this upgrade: Docker'"'"'s own disk has %.1f GB free, and %s plus the %g GB floor (BOSS_DISK_FLOOR_GB) needs at least %g GB. On Docker Desktop raise the virtual disk size (Settings > Resources) or run boss prune. %s\n", dock, what, floor, total, fix
      exit 1
    }
    if (dock == "") {
      printf "ok Disk: this host has %.1f GB free; Docker'"'"'s own disk could not be measured. %s plus the %g GB floor needs %g GB.\n", host, what, floor, total
    } else {
      printf "ok Disk: this host has %.1f GB free, Docker'"'"'s disk %.1f GB; %s plus the %g GB floor needs %g GB.\n", host, dock, what, floor, total
    }
  }'
}
# <<< boss_disk_admission
# Free GB on the filesystem holding $1 (walking up to an existing parent), or "" if unknown.
free_gb_at() {
  local path="$1" kb
  while [[ ! -e "$path" && "$path" != / && "$path" != . && -n "$path" ]]; do path="$(dirname "$path")"; done
  kb="$(df -Pk "$path" 2>/dev/null | awk 'NR==2 {print $4}')" || return 0
  if [[ "$kb" =~ ^[0-9]+$ ]]; then awk -v kb="$kb" 'BEGIN { printf "%.1f", kb / 1048576 }'; fi
  return 0
}
# Free GB on Docker's own disk: a local Linux daemon's root directory, else measured from inside
# a container of the current API image (Docker Desktop's VM); "" when neither can be read.
docker_free_gb() {
  local d=(docker) root os kb
  docker info >/dev/null 2>&1 || d=(sudo -n docker)
  IFS='|' read -r root os <<< "$("${d[@]}" info --format '{{.DockerRootDir}}|{{.OperatingSystem}}' 2>/dev/null || true)"
  if [[ "$(uname -s)" == Linux && "$root" == /* && -d "$root" && "$os" != *"Docker Desktop"* ]]; then
    free_gb_at "$root"; return 0
  fi
  kb="$("${d[@]}" run --rm --pull never --entrypoint df "boss-community-api:$1" -Pk / 2>/dev/null </dev/null | awk 'NR==2 {print $4}')" || true
  if [[ "$kb" =~ ^[0-9]+$ ]]; then awk -v kb="$kb" 'BEGIN { printf "%.1f", kb / 1048576 }'; fi
  return 0
}
# Docker's build cache in GB, from `docker system df` ("Build Cache  12.3GB"); "" when unknown.
build_cache_gb() {
  local d=(docker) size
  docker info >/dev/null 2>&1 || d=(sudo -n docker)
  size="$("${d[@]}" system df --format '{{.Type}}|{{.Size}}' 2>/dev/null | awk -F'|' '$1 ~ /Build Cache/ {print $2; exit}')" || true
  [[ -n "$size" ]] || return 0
  LC_ALL=C awk -v s="$size" 'BEGIN {
    n = s; gsub(/[^0-9.]/, "", n); u = s; gsub(/[0-9. ]/, "", u); u = tolower(u)
    if (n == "") exit
    if (u == "b") n /= 1e9; else if (u == "kb") n /= 1e6; else if (u == "mb") n /= 1e3
    else if (u == "tb") n *= 1e3
    printf "%.1f", n }'
  return 0
}
has_current_images() {
  local d=(docker)
  docker info >/dev/null 2>&1 || d=(sudo -n docker)
  if "${d[@]}" image inspect "boss-community-api:$1" >/dev/null 2>&1; then echo 1; else echo 0; fi
  return 0
}
disk_check() {
  local tag host dock cache images floor full incr verdict backups
  tag="$(image_tag)"
  floor="$(env_or_dotenv BOSS_DISK_FLOOR_GB 20)"
  full="$(env_or_dotenv BOSS_UPGRADE_FULL_BUILD_GB 35)"
  incr="$(env_or_dotenv BOSS_UPGRADE_INCREMENTAL_BUILD_GB 5)"
  for n in "$floor" "$full" "$incr"; do
    [[ "$n" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "BOSS_DISK_FLOOR_GB, BOSS_UPGRADE_FULL_BUILD_GB and BOSS_UPGRADE_INCREMENTAL_BUILD_GB must be numbers of GB (got '$n'); nothing changed." >&2; exit 2; }
  done
  backups="${BACKUP_DIR:-${BOSS_BACKUP_DIR:-$HOME/boss-backups}}"
  host="$(free_gb_at "$INSTALL_DIR")"
  local backups_free; backups_free="$(free_gb_at "$backups")"
  if [[ -n "$backups_free" ]] && { [[ -z "$host" ]] || awk -v a="$backups_free" -v b="$host" 'BEGIN { exit !(a + 0 < b + 0) }'; }; then
    host="$backups_free"
  fi
  if [[ -z "$host" ]]; then
    echo 'Disk: free space on this host could not be measured; going ahead without the disk check.' >&2
    return 0
  fi
  dock="$(docker_free_gb "$tag")"
  cache="$(build_cache_gb)"
  images="$(has_current_images "$tag")"
  if verdict="$(boss_disk_verdict "$host" "$dock" "$cache" "$images" "$floor" "$full" "$incr")"; then
    echo "${verdict#ok }"
  else
    if [[ "$DRY_RUN" -eq 1 ]]; then
      echo "Would refuse: ${verdict#refuse }"
    else
      echo "${verdict#refuse }" >&2
      exit 1
    fi
  fi
}
prune_stale_build_cache_before_check() {
  local _prune_script="$INSTALL_DIR/scripts/install/prune.sh"
  [[ "$DRY_RUN" -eq 0 && -x "$_prune_script" ]] || return 0
  local tag floor full incr images cache target_free min_cache=0
  tag="$(image_tag)"
  floor="$(env_or_dotenv BOSS_DISK_FLOOR_GB 20)"
  full="$(env_or_dotenv BOSS_UPGRADE_FULL_BUILD_GB 35)"
  incr="$(env_or_dotenv BOSS_UPGRADE_INCREMENTAL_BUILD_GB 5)"
  for n in "$floor" "$full" "$incr"; do
    [[ "$n" =~ ^[0-9]+([.][0-9]+)?$ ]] || return 0
  done
  cache="$(build_cache_gb)"
  [[ -n "$cache" ]] || return 0
  awk -v c="$cache" 'BEGIN { exit !(c + 0 > 0) }' || return 0
  images="$(has_current_images "$tag")"
  if [[ "$images" == "1" ]] && awk -v c="$cache" 'BEGIN { exit !(c + 0 >= 1) }'; then
    target_free="$(LC_ALL=C awk -v f="$floor" -v i="$incr" 'BEGIN { v = f + i; printf "%d", (v == int(v) ? v : int(v) + 1) }')"
    min_cache=1
  else
    target_free="$(LC_ALL=C awk -v f="$floor" -v u="$full" 'BEGIN { v = f + u; printf "%d", (v == int(v) ? v : int(v) + 1) }')"
  fi
  bash "$_prune_script" --dir "$INSTALL_DIR" --cache-only --room \
    --target-free "$target_free" --min-cache "$min_cache" </dev/null || true
}
if [[ "$DISK_CHECK" -eq 1 && "$(env_or_dotenv BOSS_DISK_CHECK on)" != off ]]; then
  prune_stale_build_cache_before_check
  disk_check
else
  echo 'Disk check skipped (--skip-disk-check / BOSS_DISK_CHECK=off).'
fi

# THE TEST POSTGRES STAYS UP (BOSSAPP-1335). db-test (profile `dev`) holds only test databases;
# an upgrade that stopped it killed every agent's bench-DB test run mid-suite. The installer
# never names the `dev` profile and the backup leaves kind=test services running, so db-test is
# untouched — unless its own definition or image changed, which `test_db_after` then applies.
test_db_compose() {
  local d=(docker)
  docker info >/dev/null 2>&1 || d=(sudo -n docker)
  upgrade_compose_files || return 1
  "${d[@]}" compose "${BOSS_COMPOSE_FILES[@]}" \
    --env-file "$INSTALL_DIR/.env" --profile dev "$@"
}
test_db_running() {
  test_db_compose ps -q --status running db-test 2>/dev/null </dev/null || true
}
test_db_after() {
  local before="$1" after
  [[ -n "$before" ]] || return 0
  # Recreated by Compose ONLY when db-test's definition or image changed; otherwise a no-op.
  if ! test_db_compose up -d --no-build --no-deps db-test </dev/null >/dev/null 2>&1; then
    echo 'The test Postgres (db-test) could not be brought up to date; start it with: boss dev up' >&2
    return 0
  fi
  after="$(test_db_running)"
  if [[ "$after" == "$before" ]]; then
    echo 'Left the test Postgres (db-test) running: its definition and image did not change.'
  else
    echo 'Recreated the test Postgres (db-test): its definition or image changed.'
  fi
  return 0
}
TEST_DB_BEFORE=""
if [[ "$DRY_RUN" -eq 1 ]]; then
  if [[ -n "$(test_db_running)" ]]; then
    echo 'Would leave the test Postgres (db-test) running (recreated only if its definition or image changes).'
  fi
  printf 'Would keep the running images as prev-%s (for boss rollback).\n' "$(image_tag)"
  printf 'Would back up %s, then rerun the installer from %s (%s → %s).\n' "$INSTALL_DIR" "$REF" "$OLD_COMMIT" "$NEW_COMMIT"
  exit 0
fi
keep_previous_images "$(image_tag)"

FETCHED_INSTALLER="$(mktemp)"
# The new release's post-start check, kept aside: a rollback is verified with it (below).
FETCHED_CHECK="$(mktemp)"
OLD_INSTALLER=""
trap 'rm -f "$FETCHED_INSTALLER" "$FETCHED_CHECK" ${OLD_INSTALLER:+"$OLD_INSTALLER"}' EXIT
git -C "$INSTALL_DIR" show "$NEW_COMMIT:install.sh" > "$FETCHED_INSTALLER"
git -C "$INSTALL_DIR" show "$NEW_COMMIT:scripts/install/post_check.sh" > "$FETCHED_CHECK" 2>/dev/null ||
  : > "$FETCHED_CHECK"
# The fetched installer makes this run's fresh backup before changing source.
# Pin it to the commit we already fetched so a moving branch cannot change
# between the preflight and the installer's second fetch. BOSS_RELEASE_REF_NAME tells
# the installer which ref that commit came from, so it records the release, not a bare SHA.
install_args=(--dir "$INSTALL_DIR" --ref "$NEW_COMMIT" --yes --skip-model-key)
[[ -n "$BACKUP_DIR" ]] && install_args+=(--backup-dir "$BACKUP_DIR")
[[ -n "$WEB_MODE" ]] && install_args+=("$WEB_MODE")
if ! BOSS_RELEASE_REF_NAME="$REF" bash "$FETCHED_INSTALLER" "${install_args[@]}" </dev/null; then
  echo 'Upgrade failed. The installer reports the recovery backup path above if it created one.' >&2
  # ROLL BACK: put the previous source back and start it again, so a release that cannot start on
  # this host (an API that refuses to boot, a build that fails) never leaves BOSS down. Volumes
  # are kept as they are; the new release only adds tables, which the old one ignores.
  if [[ "$ROLLBACK" -eq 1 && "$(git -C "$INSTALL_DIR" rev-parse HEAD)" != "$OLD_COMMIT" ]]; then
    echo "Putting back the previous release ($OLD_COMMIT) and starting it again..." >&2
    if git -C "$INSTALL_DIR" symbolic-ref -q HEAD >/dev/null 2>&1; then
      git -C "$INSTALL_DIR" reset -q --hard "$OLD_COMMIT" </dev/null
    else
      git -C "$INSTALL_DIR" checkout -q --detach "$OLD_COMMIT" </dev/null
    fi
    OLD_INSTALLER="$(mktemp)"
    git -C "$INSTALL_DIR" show "$OLD_COMMIT:install.sh" > "$OLD_INSTALLER"
    if BOSS_RELEASE_REF_NAME="$CURRENT_REF" bash "$OLD_INSTALLER" --dir "$INSTALL_DIR" \
         --ref "$OLD_COMMIT" --yes --skip-model-key </dev/null; then
      echo "Rolled back: $CURRENT_REF is running again. Nothing was lost." >&2
      echo "Read the error above, then see scripts/install/README.md (upgrade problems) before retrying." >&2
    elif [[ -s "$FETCHED_CHECK" ]] &&
         bash "$FETCHED_CHECK" --dir "$INSTALL_DIR" --wait "${BOSS_ROLLBACK_CHECK_WAIT:-180}" </dev/null >&2; then
      # The previous installer's own last check failed, but the previous release IS running: an
      # older installer probes with the shared BOSS_API_KEY, which Identity retires 30 days after
      # its first use. The new release's check (public /health, host tools key) passed.
      echo "Rolled back: $CURRENT_REF is running again (verified with $REF's check). Nothing was lost." >&2
      echo "Its own installer's final check failed (an older installer probes with the shared key, retired after 30 days); BOSS answers with the host tools key." >&2
      echo "Read the error above, then see scripts/install/README.md (upgrade problems) before retrying." >&2
    else
      echo 'The rollback did not start either. Restore the recovery backup: scripts/install/README.md (restore).' >&2
    fi
    exit 1
  fi
  echo 'Inspect Compose logs before retrying. See scripts/install/README.md for the recovery procedure.' >&2
  exit 1
fi
# CARRY THE LOCAL SELF-FIXES onto the new release (dropped ones are not re-applied). A stack
# that does not build or start leaves the new release running without it, and says so.
if [[ -n "$STACK_REF" && -f "$SELF_FIX" ]]; then
  if ! python3 "$SELF_FIX" --dir "$INSTALL_DIR" restack --to "$REF" ${without_args[@]+"${without_args[@]}"} </dev/null; then
    echo 'The new release runs without the local BOSS fixes; see the line above. BOSS re-ports them.' >&2
  fi
fi
# IMAGE AND BUILD-CACHE RETENTION (BOSSAPP-1147): the new release runs, so only its images and
# the prev-<tag> rollback point stay; the build cache is capped. Best effort, from the new
# checkout's prune.sh (an older release without it simply skips this). --room, as install.sh
# after its build: this upgrade just built, so a small Docker disk (or the Mac disk under a
# Colima/Desktop VM) is trimmed to keep BOSS_DISK_FLOOR_GB free. An rc10.34→rc10.35 upgrade said
# it pruned and left 17.2 GB against the 10 GB cap (ENT-DISK-02): prune.sh now prints Docker's
# own before/after reading of the build cache, and fails when it is still over the cap.
if [[ -x "$INSTALL_DIR/scripts/install/prune.sh" ]]; then
  bash "$INSTALL_DIR/scripts/install/prune.sh" --dir "$INSTALL_DIR" --room </dev/null ||
    echo 'Pruning did not finish (the lines above say what was left); run boss prune later.' >&2
fi
# VOLUME LABELS (BOSSAPP-1193): the Infra Agent removes an orphan volume only when it carries
# this install's boss.install_id and a rebuildable boss.kind, and Docker cannot add a label to a
# volume that exists. So the rebuildable volumes got labelled `_v2` keys, and the installer has
# just moved the services onto them; the new checkout's steward now removes the retired,
# unlabelled ones (the old toolchain cache; the old test Postgres once db-test has moved too)
# when no container uses them. It never stops a service and never touches a data volume. One it
# cannot free is reported by the Infra Agent as "unlabelled BOSS volume — needs relabel". Best
# effort: an older release without the step simply skips it.
if [[ -f "$INSTALL_DIR/scripts/install/steward.py" ]] && command -v python3 >/dev/null 2>&1 &&
   grep -q -- '--relabel-volumes' "$INSTALL_DIR/scripts/install/steward.py"; then
  python3 "$INSTALL_DIR/scripts/install/steward.py" --install-dir "$INSTALL_DIR" --relabel-volumes </dev/null ||
    echo 'BOSS volume labels were not checked; the Infra Agent lists any it cannot judge.' >&2
fi
# TEST LEFTOVERS (BOSSAPP-1459): the test Postgres's volume goes when no container uses it.
if [[ -f "$INSTALL_DIR/scripts/install/steward.py" ]] && command -v python3 >/dev/null 2>&1 &&
   grep -q -- '--test-leftovers' "$INSTALL_DIR/scripts/install/steward.py"; then
  python3 "$INSTALL_DIR/scripts/install/steward.py" --install-dir "$INSTALL_DIR" --test-leftovers </dev/null ||
    echo 'Test leftovers were not checked; boss prune does it later.' >&2
fi
# Read only now: the installer and its backup never touch db-test, so this is the container
# that ran through the upgrade (and a failed upgrade makes no Docker call for it at all).
TEST_DB_BEFORE="$(test_db_running)"
test_db_after "$TEST_DB_BEFORE"
printf 'Upgrade complete: %s → %s (%s)\n' "$OLD_COMMIT" "$NEW_COMMIT" "$REF"
