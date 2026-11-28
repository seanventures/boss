#!/usr/bin/env bash
# The API's memory budget before a release (BOSSAPP-1506): scripts/memory_budget.py in a
# throwaway container of the installed API image (Linux, glibc, the image's MALLOC_ARENA_MAX),
# this checkout mounted read-only, against a SCRATCH database it seeds with install-shaped builds.
#
#   BOSS_MEMORY_BUDGET_DATABASE_URL=postgresql+psycopg://boss:…@host.docker.internal:55700/boss_test_membudget \
#     ~/boss-ops/heavy.sh scripts/memory-budget.sh [--minutes 10] [--growth-mb 200]
#
# The database must be test-named (boss_test_*) and not on the install's own server; the script
# refuses anything else. Exit 1 when the resident set grows past the bound.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
url="${BOSS_MEMORY_BUDGET_DATABASE_URL:?set BOSS_MEMORY_BUDGET_DATABASE_URL to a scratch boss_test_* database}"
image="${BOSS_MEMORY_BUDGET_IMAGE:-boss-community-api:boss-community}"
exec docker run --rm --memory 3g --entrypoint python3 \
  -v "$here":/src:ro -w /src \
  -e PYTHONPATH=/src:/src/engine -e DATABASE_URL="$url" -e BOSS_ARTIFACTS_DIR=/tmp/artifacts \
  -e MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}" \
  "$image" scripts/memory_budget.py "$@"
