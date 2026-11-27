#!/usr/bin/env bash
# BOSSAPP-940 (Kiro gap 7) — Lighter first install: pull prebuilt published images by default,
# keeping source builds as an explicit option (--from-source or BOSS_BUILD_FROM_SOURCE=1) and
# falling back cleanly to scripts/install/build_images.sh if a prebuilt image cannot be pulled.
#
# Usage:
#   bash scripts/install/pull_or_build.sh [--dir INSTALL_DIR] [--registry REGISTRY] \
#        [--tag TAG] [--from-source] [--dry-run] -- docker compose -f ... --env-file .env
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DIR="."
REGISTRY="${BOSS_IMAGE_REGISTRY:-ghcr.io/seanventures}"
TAG="${BOSS_IMAGE_TAG:-latest}"
FROM_SOURCE="${BOSS_BUILD_FROM_SOURCE:-0}"
PREBUILT="${BOSS_PREBUILT_IMAGES:-1}"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) DIR="${2:?--dir needs a path}"; shift 2 ;;
    --registry) REGISTRY="${2:?--registry needs a value}"; shift 2 ;;
    --tag) TAG="${2:?--tag needs a value}"; shift 2 ;;
    --from-source) FROM_SOURCE=1; shift ;;
    --prebuilt) PREBUILT=1; FROM_SOURCE=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --) shift; break ;;
    *) printf 'pull_or_build: unknown option %s\n' "$1" >&2; exit 2 ;;
  esac
done

[[ $# -gt 0 ]] || { printf 'pull_or_build: give the compose command after --\n' >&2; exit 2; }
COMPOSE=("$@")

if [[ "$FROM_SOURCE" == "1" || "$PREBUILT" == "0" ]]; then
  printf '  • Install mode: source build (--from-source)\n'
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf 'DRY_RUN mode=source cmd=build_images.sh\n'
    exit 0
  fi
  exec bash "$HERE/build_images.sh" --dir "$DIR" -- "${COMPOSE[@]}"
fi

printf '  • Install mode: prebuilt images (%s/boss-*:%s; pass --from-source to build locally)\n' \
  "${REGISTRY%/}" "$TAG"
if [[ "$DRY_RUN" -eq 1 ]]; then
  printf 'DRY_RUN mode=prebuilt registry=%s tag=%s\n' "${REGISTRY%/}" "$TAG"
  exit 0
fi

export BOSS_IMAGE_REGISTRY="${REGISTRY%/}"
export BOSS_IMAGE_TAG="$TAG"
if "${COMPOSE[@]}" pull --ignore-buildable </dev/null 2>/dev/null || "${COMPOSE[@]}" pull </dev/null; then
  printf '  ✓ Pulled prebuilt BOSS images (%s:%s)\n' "${REGISTRY%/}" "$TAG"
  exit 0
fi

printf '  ! Prebuilt image pull was unavailable for %s:%s; falling back to local source build.\n' \
  "${REGISTRY%/}" "$TAG" >&2
exec bash "$HERE/build_images.sh" --dir "$DIR" -- "${COMPOSE[@]}"
