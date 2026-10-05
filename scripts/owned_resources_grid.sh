#!/bin/sh
# The resource x ending grid (engine/tests/test_owned_resources_are_reaped.py) where BOSS
# actually runs: Linux, in the api image, under an init (compose `init: true`), capped like a
# build envelope. Finding an orphan by the token its environment carries needs /proc, which a
# developer's Mac does not give, so the Mac run skips those cases and this one runs them all.
#
#   scripts/owned_resources_grid.sh                 # api image built by `make dev`
#   BOSS_TEST_DATABASE_URL=postgresql+psycopg://u:p@host.docker.internal:5432/db_test \
#     scripts/owned_resources_grid.sh               # + the database-connection cases
#   DOCKER_SOCKET=1 scripts/owned_resources_grid.sh # + the container/image cases
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
image=${BOSS_API_IMAGE:-boss-community-api:boss-community}
db=$(printf '%s' "${BOSS_TEST_DATABASE_URL:-}" | sed 's#@127\.0\.0\.1:#@host.docker.internal:#; s#@localhost:#@host.docker.internal:#')
sock=""
if [ "${DOCKER_SOCKET:-}" = 1 ]; then
  sock="-v /var/run/docker.sock:/var/run/docker.sock --group-add 0"
fi
# shellcheck disable=SC2086
exec docker run --rm --init --memory 1g --pids-limit 512 \
  --add-host host.docker.internal:host-gateway $sock \
  -e BOSS_TEST_DATABASE_URL="$db" -e PYTHONPATH=/src/engine \
  -v "$here/engine:/src/engine:ro" -w /src --entrypoint python "$image" \
  -m pytest -p no:cacheprovider -q engine/tests/test_owned_resources_are_reaped.py "$@"
