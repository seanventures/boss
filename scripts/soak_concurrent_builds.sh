#!/bin/sh
# The soak (tests/soak/test_concurrent_builds_leave_nothing.py): six builds in the api image,
# capped at 3 GB / 2 CPUs under an init, against a throwaway database. Run on demand or nightly:
#
#   BOSS_TEST_DATABASE_URL=postgresql+psycopg://u:p@127.0.0.1:5432/boss_soak_test \
#     scripts/soak_concurrent_builds.sh
#
# BOSS_SOAK_ONLY=cancel|six runs one case. Prints `SOAK_REPORT {json}`. The container is removed when it ends (--rm), and everything the
# builds made lived inside it.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
image=${BOSS_API_IMAGE:-boss-community-api:boss-community}
db=$(printf '%s' "${BOSS_TEST_DATABASE_URL:?set BOSS_TEST_DATABASE_URL}" | sed 's#@127\.0\.0\.1:#@host.docker.internal:#; s#@localhost:#@host.docker.internal:#')
exec docker run --rm --init --name "boss-soak-$$" --label boss.soak=1 \
  --memory 3g --memory-swap 3g --cpus 2 --pids-limit 4096 \
  --add-host host.docker.internal:host-gateway \
  -e BOSS_SOAK=1 -e BOSS_SOAK_ONLY="${BOSS_SOAK_ONLY:-}" -e BOSS_TEST_DATABASE_URL="$db" -e PYTHONPATH=/src/engine \
  -e BOSS_ENGINE_DIR=/src/engine -e BOSS_DB_POOL_SIZE=5 \
  -v "$here/engine:/src/engine:ro" -v "$here/db:/src/db:ro" -v "$here/tests/soak:/src/tests/soak:ro" \
  -w /src --entrypoint python "$image" \
  -m pytest -p no:cacheprovider -q -s tests/soak/test_concurrent_builds_leave_nothing.py
