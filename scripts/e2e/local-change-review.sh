#!/usr/bin/env bash
# The click-by-click change-review suites (BOSSAPP-948/954) in one command, on a disposable stack:
#
#   scripts/e2e/local-change-review.sh                 both suites, at 1440 and 390
#   scripts/e2e/local-change-review.sh -k tabs -x      extra arguments go to pytest
#   BOSS_E2E_SUITES=tests/e2e/local_change_review/test_git_review.py scripts/e2e/local-change-review.sh
#   BOSS_E2E_KEEP=1 …                                  leave the stack up afterwards (prints how to stop it)
#
# What it starts, each on its own port (never the install's 3000/8000) and stops on exit:
#   * a fresh database boss_test_lcr (BOSS_E2E_DB) on the local Postgres (localhost:5432), made
#     with the product's own ensure_schema + seed; the password is read from ~/boss/.env into a
#     variable only (or BOSS_E2E_DB_PASSWORD), never printed;
#   * the plumbing gate's fakes (tests/e2e/stubs/fakes.py): GitHub, git-backed;
#   * the dev API (scripts/e2e/devapi.py): this checkout's real API with the dev seams;
#   * the console from this checkout (`next dev`).
# Logs, the import/repo roots and failure screenshots go to BOSS_E2E_STATE (a new temp folder).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"
API_PORT="${BOSS_E2E_API_PORT:-8873}"
WEB_PORT="${BOSS_E2E_WEB_PORT:-3873}"
FAKES_PORT="${BOSS_E2E_FAKES_PORT:-8919}"
DB="${BOSS_E2E_DB:-boss_test_lcr}"
STATE="${BOSS_E2E_STATE:-$(mktemp -d -t boss-e2e-lcr)}"
SUITES="${BOSS_E2E_SUITES:-tests/e2e/local_change_review/test_local_change_review.py tests/e2e/local_change_review/test_git_review.py}"

[[ "$DB" =~ ^boss_test_[a-z0-9_]+$ ]] || { echo "BOSS_E2E_DB must be named boss_test_*"; exit 2; }
for port in "$API_PORT" "$WEB_PORT" "$FAKES_PORT"; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then echo "port $port is in use"; exit 2; fi
done
free_gb=$(df -Pk / | awk 'NR==2 {print int($4 / 1048576)}')
if (( free_gb < 10 )); then echo "only ${free_gb} GB free on /: refusing to start (needs 10)"; exit 2; fi

mkdir -p "$STATE"/{repos,import,fakes,shots,artifacts}
chmod 700 "$STATE"
PW="${BOSS_E2E_DB_PASSWORD:-$(grep '^BOSS_DB_PASSWORD=' "$HOME/boss/.env" | cut -d= -f2-)}"
[[ -n "$PW" ]] || { echo "no database password (BOSS_E2E_DB_PASSWORD or ~/boss/.env)"; exit 2; }
DB_URL="postgresql+psycopg://boss:${PW}@localhost:5432/${DB}"
unset PW
export PYTHONPATH="engine:apps/api:apps/worker:."
export BOSS_DEFAULT_ORG_ID=org_boss BOSS_DEFAULT_WORKSPACE_ID=ws_default BOSS_DEFAULT_PROJECT_ID=helpdesk-ai-demo
export BOSS_ARTIFACTS_DIR="$STATE/artifacts" BOSS_EDITION=community

# `next dev` rewrites these on start (and writes apps/web/AGENTS.md): put back what was clean.
GENERATED="apps/web/next-env.d.ts apps/web/tsconfig.json"
RESTORE=0; git diff --quiet -- $GENERATED 2>/dev/null && RESTORE=1
AGENTS_NEW=0; [[ -e apps/web/AGENTS.md ]] || AGENTS_NEW=1

PIDS=()
kill_tree() {   # a process and everything it started (next dev forks workers)
  local pid=$1 child
  for child in $(pgrep -P "$pid" 2>/dev/null); do kill_tree "$child"; done
  kill "$pid" 2>/dev/null || true
}
stop() {
  local code=$?
  if [[ "${BOSS_E2E_KEEP:-}" == "1" ]]; then
    echo "stack kept: kill ${PIDS[*]:-} (and their children); state in $STATE"
    exit $code
  fi
  for pid in "${PIDS[@]:-}"; do [[ -n "$pid" ]] && kill_tree "$pid"; done
  sleep 1
  for pid in "${PIDS[@]:-}"; do [[ -n "$pid" ]] && kill -9 "$pid" 2>/dev/null || true; done
  DB_URL="$DB_URL" DB="$DB" "$PY" - <<'EOF' || true
import os
from sqlalchemy import create_engine, text
url = os.environ["DB_URL"].rsplit("/", 1)[0] + "/postgres"
eng = create_engine(url, isolation_level="AUTOCOMMIT")
with eng.connect() as c:
    c.execute(text(f'DROP DATABASE IF EXISTS "{os.environ["DB"]}" WITH (FORCE)'))
eng.dispose()
EOF
  (( RESTORE )) && git checkout -q -- $GENERATED 2>/dev/null || true
  (( AGENTS_NEW )) && rm -f apps/web/AGENTS.md || true
  echo "stopped; logs and failure screenshots in $STATE"
  exit $code
}
trap stop EXIT INT TERM

wait_for() {   # url, seconds, what
  local end=$((SECONDS + $2))
  until curl -s -o /dev/null --max-time 5 "$1"; do
    (( SECONDS < end )) || { echo "$3 did not come up; see $STATE"; exit 1; }
    sleep 2
  done
}

echo "· database $DB (fresh)"
DB_URL="$DB_URL" DB="$DB" "$PY" - <<'EOF'
import os
from sqlalchemy import create_engine, text
db = os.environ["DB"]
admin = create_engine(os.environ["DB_URL"].rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
with admin.connect() as c:
    c.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
    c.execute(text(f'CREATE DATABASE "{db}"'))
admin.dispose()
os.environ["DATABASE_URL"] = os.environ["DB_URL"]
from boss_engine.bootstrap import seed
seed()
EOF

echo "· fakes on :$FAKES_PORT"
"$PY" tests/e2e/stubs/fakes.py --port "$FAKES_PORT" --state-dir "$STATE/fakes" >"$STATE/fakes.log" 2>&1 &
PIDS+=($!)

echo "· dev API on :$API_PORT"
DATABASE_URL="$DB_URL" BOSS_E2E_STATE="$STATE" BOSS_LOCAL_REPO_ROOT="$STATE/repos" \
  BOSS_LOCAL_IMPORT_ROOT="$STATE/import" LF_FAKES="http://127.0.0.1:$FAKES_PORT" \
  BOSS_PUBLIC_URL="http://localhost:$WEB_PORT" \
  "$PY" scripts/e2e/devapi.py "$API_PORT" >"$STATE/api.log" 2>&1 &
PIDS+=($!)

echo "· console on :$WEB_PORT (next dev)"
(cd apps/web && BOSS_API_URL="http://127.0.0.1:$API_PORT" NEXT_PUBLIC_BOSS_API_URL="http://127.0.0.1:$API_PORT" \
  exec npx next dev -p "$WEB_PORT") >"$STATE/web.log" 2>&1 &
PIDS+=($!)

wait_for "http://127.0.0.1:$FAKES_PORT/_mail" 60 "the fakes"
wait_for "http://127.0.0.1:$API_PORT/__dev/state" 180 "the dev API"
wait_for "http://localhost:$WEB_PORT/" 240 "the console"
# The first page compile in next dev takes minutes: warm the change page's route once.
curl -s -o /dev/null --max-time 300 "http://localhost:$WEB_PORT/changes/x/1" || true

echo "· suites: $SUITES"
set +e
BOSS_E2E_DATABASE_URL="$DB_URL" BOSS_E2E_CONSOLE="http://localhost:$WEB_PORT" \
  BOSS_E2E_API="http://127.0.0.1:$API_PORT" BOSS_E2E_DEV_SEAMS=1 \
  BOSS_E2E_IMPORT_ROOT="$STATE/import" BOSS_E2E_REPO_ROOT="$STATE/repos" \
  BOSS_E2E_FAKES="http://127.0.0.1:$FAKES_PORT" BOSS_E2E_SHOTS="$STATE/shots" \
  "$PY" -m pytest -p no:cacheprovider -p no:warnings -q -rf --tb=short $SUITES "$@"
code=$?
set -e
exit $code
