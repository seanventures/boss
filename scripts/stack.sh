#!/usr/bin/env bash
# BOSS local stack: start everything, check everything, keep it up.
#
# WHY THIS EXISTS
# ---------------
# BOSS is four processes (postgres, api, worker, web) and only three of them are visible.
# The worker is the invisible one: nothing on screen changes when it is missing, so meetings
# sit at "running" until a watchdog fails them ~10 minutes later, and builds never start.
# A meeting that produced no PRD for 20 minutes was diagnosed as a meeting regression twice
# before anyone checked whether a worker existed at all -- it had been absent for six days.
#
# The failure was silent in BOTH directions: the worker also polls a control-plane URL from
# .env, and when that pointed at a dead port it logged connection errors into a file nobody
# reads while the console showed a perfectly healthy-looking queue.
#
#   ./scripts/stack.sh up       start whatever is missing, verify each one answers
#   ./scripts/stack.sh status   one line per process; exit 1 if anything is down
#   ./scripts/stack.sh watch    keep checking, restart what dies
set -uo pipefail
cd "$(dirname "$0")/.."

API_PORT="${BOSS_API_PORT:-8000}"
WEB_PORT="${BOSS_WEB_PORT:-3001}"
PY="${PYTHON_BIN:-./.venv/bin/python}"
LOGS="${BOSS_LOG_DIR:-/tmp/boss-stack}"; mkdir -p "$LOGS"

listening() { lsof -tiTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
worker_pid() { pgrep -f "apps.worker.boss_worker.main" | head -1; }

# The worker holds no port, so "is it up" has to be asked of its work, not of a socket.
# A job that has been queued longer than this with no worker process is the real symptom.
STALE_JOB_SECONDS="${BOSS_STALE_JOB_SECONDS:-120}"
oldest_queued() {
  docker exec infra-db-1 psql -U boss -d boss -t -A -c \
    "select coalesce(max(extract(epoch from now()-created_at))::int,0) from jobs where status='queued';" 2>/dev/null | tr -d ' '
}

check_db()     { docker exec infra-db-1 pg_isready -U boss >/dev/null 2>&1; }
check_api()    { curl -fsS -m 5 "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1 || listening "$API_PORT"; }
check_worker() { [ -n "$(worker_pid)" ]; }
check_web()    { listening "$WEB_PORT"; }

start_api() {
  listening "$API_PORT" && return 0
  echo "  starting api on :$API_PORT"
  PYTHONPATH=.:engine nohup $PY -m uvicorn apps.api.app.main:app \
    --host 0.0.0.0 --port "$API_PORT" >>"$LOGS/api.log" 2>&1 &
  for _ in $(seq 1 40); do check_api && return 0; sleep 1; done
  echo "  ! api did not come up — see $LOGS/api.log"; return 1
}

start_worker() {
  check_worker && return 0
  # The mistake this guards: .env pointed the worker at a port nothing served, so it ran
  # happily and did nothing. Say where it is dialling, and refuse to pretend that is fine.
  local target; target="$(grep -E '^BOSS_CONTROL_PLANE_URL=' .env 2>/dev/null | cut -d= -f2-)"
  target="${target:-http://127.0.0.1:$API_PORT}"
  if ! curl -fsS -m 5 "$target/health" >/dev/null 2>&1 && ! curl -fsS -m 5 "$target/" >/dev/null 2>&1; then
    echo "  ! worker would dial $target, which is not answering."
    echo "    Fix BOSS_CONTROL_PLANE_URL in .env (the api is on :$API_PORT) and re-run."
    return 1
  fi
  echo "  starting worker (control plane $target)"
  PYTHONPATH=.:engine nohup $PY -m apps.worker.boss_worker.main >>"$LOGS/worker.log" 2>&1 &
  for _ in $(seq 1 20); do check_worker && return 0; sleep 1; done
  echo "  ! worker did not stay up — see $LOGS/worker.log"; return 1
}

start_web() {
  listening "$WEB_PORT" && return 0
  echo "  starting web on :$WEB_PORT"
  (cd apps/web && PORT="$WEB_PORT" nohup npx next start -p "$WEB_PORT" >>"$LOGS/web.log" 2>&1 &)
  for _ in $(seq 1 60); do check_web && return 0; sleep 1; done
  echo "  ! web did not come up — see $LOGS/web.log"; return 1
}

status() {
  local bad=0 line
  for svc in db api worker web; do
    if check_$svc; then line="up"; else line="DOWN"; bad=1; fi
    printf '  %-7s %s\n' "$svc" "$line"
  done
  # The check that would have caught this whole incident on day one.
  local age; age="$(oldest_queued)"; age="${age:-0}"
  if [ "$age" -gt "$STALE_JOB_SECONDS" ]; then
    if check_worker; then
      printf '  %-7s %s\n' "queue" "BACKED UP — oldest job queued ${age}s; a worker is up but has not reached it"
    else
      printf '  %-7s %s\n' "queue" "STUCK — oldest job queued ${age}s and NO worker is running"
    fi
    bad=1
  else
    printf '  %-7s %s\n' "queue" "draining (oldest queued ${age}s)"
  fi
  return $bad
}

case "${1:-up}" in
  up)
    check_db || { echo "  ! postgres (infra-db-1) is not running: docker compose -f infra/docker-compose.yml up -d"; exit 1; }
    start_api; start_worker; start_web; echo; status ;;
  status) status ;;
  watch)
    # Auto-recovery. Each pass restarts only what is actually missing, so this is safe to
    # leave running; it is also the only thing that notices a worker dying at 3am.
    while true; do
      check_api    || { echo "$(date +%T) api down — restarting";    start_api; }
      check_worker || { echo "$(date +%T) worker down — restarting"; start_worker; }
      check_web    || { echo "$(date +%T) web down — restarting";    start_web; }
      sleep "${BOSS_WATCH_INTERVAL:-30}"
    done ;;
  *) echo "usage: $0 {up|status|watch}"; exit 2 ;;
esac
