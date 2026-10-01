#!/usr/bin/env bash
# Back up a running Community install, then move it forward along the ref it follows.
set -euo pipefail
umask 077

# This script also runs piped (bash -s) against older installs, so it carries its own copy of
# the release rules. The awk program between the markers is identical in install.sh and
# docs/landing/install.sh; a test keeps the copies equal.
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

INSTALL_DIR="${BOSS_INSTALL_DIR:-$HOME/boss}"
REF="${BOSS_REPO_REF:-}"
BACKUP_DIR=""
WEB_MODE=""
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: upgrade.sh [--dir INSTALL_DIR] [--ref REF] [--backup-dir PATH]
                  [--web | --no-web] [--dry-run]

Without --ref, an install stays on the channel it was installed from:
  release  moves to the newest published release tag (v*), never to main
  branch   fast-forwards that branch (main for installs made before releases were recorded)
  pinned   a commit install needs --ref to move
The installed ref is recorded in .boss-release; `boss upgrade` runs this script.

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
    --ref) REF="${2:?--ref needs a ref}"; shift 2 ;;
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
CURRENT_REF="$(installed_ref "$INSTALL_DIR")"
if [[ -z "$REF" ]]; then
  case "$(ref_channel "$CURRENT_REF")" in
    release)
      # The newest release tag, and never an older one: the installed tag is a candidate.
      # A stable install moves only to a stable release; a prerelease install may also move
      # to a newer prerelease.
      include_pre=0
      [[ "$CURRENT_REF" != *-* ]] || include_pre=1
      tags="$(git -C "$INSTALL_DIR" ls-remote --tags --refs origin 'v*' </dev/null | sed 's#.*refs/tags/##')" ||
        { echo 'Could not list releases from origin; nothing changed.' >&2; exit 1; }
      REF="$(printf '%s\n%s\n' "$tags" "$CURRENT_REF" | pick_release "$include_pre")"
      [[ -n "$REF" ]] || REF="$CURRENT_REF"
      if [[ "$REF" == "$CURRENT_REF" ]]; then
        echo "Already on the newest release, $CURRENT_REF; verifying with a safe installer rerun."
      else
        echo "Newest release: $REF (installed: $CURRENT_REF)."
      fi ;;
    pinned)
      echo "This install is pinned to commit $CURRENT_REF. Pass --ref (boss upgrade --to REF) to move it; nothing changed." >&2
      exit 1 ;;
    *) REF="$CURRENT_REF" ;;
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

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf 'Would back up %s, then rerun the installer from %s (%s → %s).\n' "$INSTALL_DIR" "$REF" "$OLD_COMMIT" "$NEW_COMMIT"
  exit 0
fi

FETCHED_INSTALLER="$(mktemp)"
trap 'rm -f "$FETCHED_INSTALLER"' EXIT
git -C "$INSTALL_DIR" show "$NEW_COMMIT:install.sh" > "$FETCHED_INSTALLER"
# The fetched installer makes this run's fresh backup before changing source.
# Pin it to the commit we already fetched so a moving branch cannot change
# between the preflight and the installer's second fetch. BOSS_RELEASE_REF_NAME tells
# the installer which ref that commit came from, so it records the release, not a bare SHA.
install_args=(--dir "$INSTALL_DIR" --ref "$NEW_COMMIT" --yes --skip-model-key)
[[ -n "$BACKUP_DIR" ]] && install_args+=(--backup-dir "$BACKUP_DIR")
[[ -n "$WEB_MODE" ]] && install_args+=("$WEB_MODE")
if ! BOSS_RELEASE_REF_NAME="$REF" bash "$FETCHED_INSTALLER" "${install_args[@]}" </dev/null; then
  echo 'Upgrade failed. The installer reports the recovery backup path above if it created one.' >&2
  echo 'Inspect Compose logs before retrying. See scripts/install/README.md for the recovery procedure.' >&2
  exit 1
fi
printf 'Upgrade complete: %s → %s (%s)\n' "$OLD_COMMIT" "$NEW_COMMIT" "$REF"
