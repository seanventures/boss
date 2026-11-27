#!/usr/bin/env bash
# Build BOSS's images the way the installer does (BOSSAPP-1458), for every other path that builds
# them: `boss toolchains enable|disable`, a remote update (remote.sh) and the self-fix installer.
#
#   bash scripts/install/build_images.sh [--dir INSTALL_DIR] [--only "svc svc"] \
#        [--build-arg K=V]... -- docker compose -f ... --env-file .env [--profile P]...
#
# Everything after `--` is the compose command up to (not including) `build`. Runs from the
# source tree whose images it builds (scripts/install/ here is that tree's).
#   * the order: scripts/install/build_order.py: each image ONCE (kg-mcp and worker share an
#     image), each image family back to back while its cache is warm;
#   * one image at a time: a single multi-service build exports several large images at once;
#   * between families, on a small disk, the finished family's build cache is dropped
#     (prune.sh --room --cache-only): a 30 GB box ran out of disk holding the Python family's
#     cache while the console built.
# Exit 0 when every image built; otherwise the failing build's status, after saying which image.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DIR="."
ONLY=""
BUILD_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) DIR="${2:?--dir needs a path}"; shift 2 ;;
    --only) ONLY="${2-}"; shift 2 ;;
    --build-arg) BUILD_ARGS+=(--build-arg "${2:?--build-arg needs K=V}"); shift 2 ;;
    --) shift; break ;;
    *) printf 'build_images: unknown option %s\n' "$1" >&2; exit 2 ;;
  esac
done
[[ $# -gt 0 ]] || { printf 'build_images: give the compose command after --\n' >&2; exit 2; }
COMPOSE=("$@")
# BOSS'S OWN BUILDER (BOSS_BUILDX_BUILDER): the images build in it, which is what makes its cache
# BOSS's to clear (prune.sh, the steward). sudo resets the environment, so env goes after sudo.
BUILDER="${BOSS_BUILDX_BUILDER:-$(grep -m 1 '^BOSS_BUILDX_BUILDER=' "$DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d "\"'" || true)}"
if [[ -n "$BUILDER" ]]; then
  if [[ "${COMPOSE[0]}" == sudo ]]; then
    i=1
    while [[ $i -lt ${#COMPOSE[@]} && "${COMPOSE[$i]}" == -* ]]; do i=$((i + 1)); done
    COMPOSE=("${COMPOSE[@]:0:$i}" env "BUILDX_BUILDER=$BUILDER" "${COMPOSE[@]:$i}")
  else
    COMPOSE=(env "BUILDX_BUILDER=$BUILDER" "${COMPOSE[@]}")
  fi
fi

# The profiles the compose command selects, for build_order's profile filter.
PROFILES=()
for ((i = 0; i < ${#COMPOSE[@]}; i++)); do
  [[ "${COMPOSE[$i]}" == --profile ]] && PROFILES+=(--profile "${COMPOSE[$((i + 1))]:-}")
done

# The configuration holds resolved credentials: parsed in memory, never printed.
if ! ORDER="$("${COMPOSE[@]}" config --format json </dev/null 2>/dev/null |
      python3 "$HERE/build_order.py" ${PROFILES[@]+"${PROFILES[@]}"} --only "$ONLY")"; then
  printf 'The Compose configuration could not be read, so no image was built. Check it with: docker compose config\n' >&2
  exit 1
fi

family=""
while read -r service next; do
  [[ -n "$service" ]] || continue
  if [[ -n "$family" && "$next" != "$family" ]]; then
    bash "$HERE/prune.sh" --dir "$DIR" --room --cache-only </dev/null 2>&1 | grep 'under the .* floor' || true
  fi
  family="$next"
  printf '  • Building %s (one image at a time)\n' "$service"
  rc=0
  "${COMPOSE[@]}" build ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} "$service" </dev/null || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    printf 'The %s image did not build (its build output is above); the running images are unchanged. Fix the cause shown there, then run `boss update`.\n' "$service" >&2
    exit "$rc"
  fi
done <<< "$ORDER"
