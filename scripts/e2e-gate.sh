#!/usr/bin/env bash
# The end-to-end release gate (docs/operations/TEST_TIERS.md).
#
#   scripts/e2e-gate.sh plumbing [--boss-root DIR] [--image IMAGE] [--keep]
#       Tier A, <= 5 min, no model calls: BOSS's real post-agent pipeline on a known-good app
#       (tests/e2e/fixtures/helpdesk-mini): clone through a GitHub 502, package.json hardening,
#       the clean install as the run's uid, the web gate composed from what the app defines (and
#       on a second, plain Node ESM fixture: tests/e2e/fixtures/plain-node), the app-start proof with the connector's
#       wiring smoke, the connector's own suite, the blocked-build words, the pull request
#       through a 502, a deploy to a local target, the retry policy, and an approval that needs
#       step-up. One PASS/FAIL table; exit 1 on any FAIL.
#   scripts/e2e-gate.sh engines [--boss-root DIR] [--image IMAGE] [--keep]
#       Tier A, no model calls: BOSS's REAL governed build pipeline end to end on the scripted
#       conformance engine (engine/boss_engine/services/scripted_engine.py), as the poorest engine
#       (no sessions, no read-only mode) and the richest (every Claude Code capability), over a
#       clean build, a failing test, a reviewer BLOCKER, a server that crashes on start and an
#       engine that cannot run. Every engine must show the same stages, gates, fix rounds, PR,
#       events, profile fields and failure words (tests/e2e/gate/engines.py). Same container.
#   scripts/e2e-gate.sh journeys [--boss-root DIR] [--image IMAGE] [--keep]
#       Tier A, no model calls: the golden journeys' runner (engine/boss_engine/services/
#       golden_journeys, BOSSAPP-1284) plays every journey that runs on the scripted engine
#       against BOSS's REAL API app, and each must come out green on the Acceptance Agent's
#       verdict (tests/e2e/gate/journeys.py). Same container and world as `engines`.
#   --inner-sandbox seccomp|seccomp+apparmor  (plumbing, engines) create the gate container with the
#       security profiles an install with BOSS_ENGINE_INNER_SANDBOX gives its api and worker
#       (scripts/install/compose_files.sh): BOSS's seccomp profile, plus AppArmor `boss-engine`
#       (loaded on the host first: scripts/sandbox/load_apparmor.sh). The enterprise lane
#       (scripts/enterprise-lane.sh) runs both tiers this way. Default: Docker's own profiles.
#   --enterprise-env FILE  (plumbing, engines) give the gate the company network an install's .env
#       names: the package mirrors, the outbound proxy and the extra CA (docs/community/
#       ENTERPRISE_NETWORK.md). Only those settings are read; nothing else in FILE is used. Without
#       it, a box whose only way out is a proxy fails the gate's dependency installs.
#   scripts/e2e-gate.sh agent [--brief <name>] [--with-connector] [--budget-min N] [--keep]
#       Tier B: ONE real, model-driven build of a known-good brief (tests/e2e/briefs/), started
#       on the local install as the test owner through BOSS's API, watched to an opened PR,
#       asserted, timed, and cleaned up after a PASS (scripts/e2e_gate_agent.py).
#
# WHERE IT RUNS. In a THROWAWAY container of the installed worker image (it has boss-sandbox-exec,
# the run uid pool, Node and npm: commands run as a build's own uid, umask 007), next to a
# throwaway Postgres, on a private Docker network; all three are removed afterwards. The
# install's own containers are never restarted, upgraded or written to. The BOSS under test is
# this checkout (or --boss-root: an older commit's checkout, to prove the gate catches its bugs),
# mounted read-only; the gate's own code and fixture always come from this checkout.
#
# Credentials: the throwaway Postgres gets a random password that lives only in a 0600 env file
# in a temporary directory; nothing prints it.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TIER="${1:-}"; shift || true

case "$TIER" in
  plumbing|engines|journeys) ;;
  agent) exec python3 "$ROOT/scripts/e2e_gate_agent.py" "$@" ;;
  *) echo "usage: scripts/e2e-gate.sh plumbing|engines|journeys [--boss-root DIR] [--image IMAGE] [--keep] [--inner-sandbox MODE]" >&2
     echo "       scripts/e2e-gate.sh agent [--brief <name>] [--with-connector] [--budget-min N] [--keep]" >&2; exit 2 ;;
esac

BOSS_ROOT="$ROOT"
IMAGE=""
KEEP=0
INNER=off
ENT_ENV=""
while [ $# -gt 0 ]; do
  case "$1" in
    --boss-root) BOSS_ROOT="$(cd "${2:?--boss-root needs a directory}" && pwd)"; shift 2 ;;
    --image) IMAGE="${2:?--image needs an image}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --enterprise-env) ENT_ENV="${2:?--enterprise-env needs an install .env}"; shift 2
      [ -r "$ENT_ENV" ] || { echo "e2e-gate: cannot read $ENT_ENV" >&2; exit 2; } ;;
    --inner-sandbox)
      case "${2:-}" in
        off|seccomp|seccomp+apparmor) INNER="$2"; shift 2 ;;
        *) echo "--inner-sandbox takes off, seccomp or seccomp+apparmor" >&2; exit 2 ;;
      esac ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

started=$(date +%s)
command -v docker >/dev/null || { echo "e2e-gate: docker is not available" >&2; exit 2; }
docker info >/dev/null 2>&1 || { echo "e2e-gate: the Docker daemon is not answering" >&2; exit 2; }
if [ -z "$IMAGE" ]; then
  IMAGE="$(docker inspect -f '{{.Config.Image}}' boss-community-worker-1 2>/dev/null || true)"
  [ -n "$IMAGE" ] || IMAGE="boss-community-worker:boss-community"
fi
docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "e2e-gate: no worker image $IMAGE (install BOSS, or pass --image)" >&2; exit 2; }
free_gb=$(df -Pk "${TMPDIR:-/tmp}" | awk 'NR==2 {print int($4/1048576)}')
if [ "${free_gb:-0}" -lt 5 ]; then
  echo "e2e-gate: only ${free_gb} GB free; need 5 GB" >&2; exit 2
fi
[ -f "$BOSS_ROOT/engine/boss_engine/__init__.py" ] || {
  echo "e2e-gate: $BOSS_ROOT is not a BOSS checkout" >&2; exit 2; }
# The install's engine inner-sandbox profiles (the same options compose_files.sh writes).
SECURITY=()
if [ "$INNER" != off ]; then
  [ -f "$ROOT/scripts/sandbox/engine-seccomp.json" ] || {
    echo "e2e-gate: $ROOT/scripts/sandbox/engine-seccomp.json is missing" >&2; exit 2; }
  SECURITY=(--security-opt "seccomp=$ROOT/scripts/sandbox/engine-seccomp.json"
            -e "BOSS_ENGINE_INNER_SANDBOX=$INNER")
  if [ "$INNER" = seccomp+apparmor ]; then
    # The profile list is readable by root only; when this user cannot read it, Docker says so.
    if _aa="$(cat /sys/kernel/security/apparmor/profiles 2>/dev/null)" &&
       ! grep -q '^boss-engine (' <<<"$_aa"; then
      echo "e2e-gate: AppArmor profile boss-engine is not loaded: sudo scripts/sandbox/load_apparmor.sh" >&2; exit 2
    fi
    SECURITY+=(--security-opt apparmor=boss-engine)
  fi
fi

ID="$(date +%s)-$$"
NET="boss-e2egate-$ID"
PG="boss-e2egate-pg-$ID"
GATE="boss-e2egate-$ID"
WORK="$(mktemp -d -t boss-e2egate-XXXX)"
chmod 700 "$WORK"
mkdir -p "$WORK/out"

cleanup() {
  # -v: the Postgres image declares a data VOLUME, and `rm -f` without it left a ~435 MB
  # anonymous volume behind on every run (BOSSAPP-1459). The label sweeps any a crash left.
  docker rm -f -v "$GATE" >/dev/null 2>&1 || true
  docker rm -f -v "$PG" >/dev/null 2>&1 || true
  docker volume ls -q --filter label=boss-e2egate=1 --filter dangling=true 2>/dev/null |
    xargs docker volume rm >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -f "$WORK/pg.env" "$WORK/gate.env"
  if [ "$KEEP" = 1 ]; then echo "e2e-gate: kept $WORK/out"; else rm -rf "$WORK"; fi
}
trap cleanup EXIT INT TERM

PW="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
umask 077
printf 'POSTGRES_PASSWORD=%s\n' "$PW" > "$WORK/pg.env"
printf 'BOSS_E2E_GATE_PG_URL=postgresql://postgres:%s@%s:5432/postgres\n' "$PW" "$PG" > "$WORK/gate.env"
umask 022
unset PW

docker network create "$NET" >/dev/null
docker run -d --name "$PG" --network "$NET" --env-file "$WORK/pg.env" \
  --label boss-e2egate=1 --mount type=volume,dst=/var/lib/postgresql/data,volume-label=boss-e2egate=1 \
  postgres:16-alpine >/dev/null || { echo "e2e-gate: could not start Postgres" >&2; exit 2; }
for _ in $(seq 1 60); do
  docker exec "$PG" pg_isready -U postgres -q >/dev/null 2>&1 && break
  sleep 0.5
done

# The company network (an install's enterprise block), when asked: URLs only, never a credential
# (the installer refuses a URL carrying one), and the CA certificate, mounted read-only.
ENTERPRISE=()
ENTRY=()
if [ -n "$ENT_ENV" ]; then
  for key in BOSS_NPM_REGISTRY BOSS_PIP_INDEX_URL BOSS_UPSTREAM_PROXY BOSS_UPSTREAM_HTTP_PROXY \
             BOSS_UPSTREAM_NO_PROXY BOSS_EGRESS_PRIVATE_ALLOW BOSS_GOPROXY BOSS_MAVEN_MIRROR; do
    val="$(grep -m 1 "^$key=" "$ENT_ENV" | cut -d= -f2- || true)"
    [ -z "$val" ] || ENTERPRISE+=(-e "$key=$val")
  done
  proxy="$(grep -m 1 '^BOSS_UPSTREAM_PROXY=' "$ENT_ENV" | cut -d= -f2- || true)"
  if [ -n "$proxy" ]; then
    no_proxy="localhost,127.0.0.1,$PG"
    extra="$(grep -m 1 '^BOSS_UPSTREAM_NO_PROXY=' "$ENT_ENV" | cut -d= -f2- || true)"
    [ -z "$extra" ] || no_proxy="$no_proxy,$extra"
    ENTERPRISE+=(-e "HTTPS_PROXY=$proxy" -e "https_proxy=$proxy" -e "NO_PROXY=$no_proxy" -e "no_proxy=$no_proxy")
  fi
  ca="$(grep -m 1 '^BOSS_EXTRA_CA_FILE=' "$ENT_ENV" | cut -d= -f2- || true)"
  if [ -n "$ca" ] && [ -s "$ca" ]; then
    ENTERPRISE+=(-v "$ca:/etc/boss/extra-ca.pem:ro")
    ENTRY=(/usr/local/libexec/boss/with-extra-ca)
  fi
fi

echo "== e2e-gate $TIER: BOSS $(git -C "$BOSS_ROOT" rev-parse --short HEAD 2>/dev/null || echo '?') in $IMAGE$([ "$INNER" = off ] || echo " (inner sandbox $INNER)")"
docker run --name "$GATE" --network "$NET" --label boss-e2egate=1 ${SECURITY[@]+"${SECURITY[@]}"} ${ENTERPRISE[@]+"${ENTERPRISE[@]}"} \
  --entrypoint "" -w /tmp --env-file "$WORK/gate.env" \
  -e BOSS_SANDBOX_ISOLATION=required -e BOSS_SANDBOX_NETWORK=off \
  -e BOSS_BUILD_SERVICES_PREFIX=boss_e2egate_ -e BOSS_ARTIFACTS_DIR=/tmp/boss-e2egate-artifacts \
  -e PYTHONPATH=/boss:/boss/engine -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$BOSS_ROOT":/boss:ro -v "$ROOT":/gate-src:ro \
  "$IMAGE" sh -c 'mkdir -p /tmp/boss-gate-output && exec "$@"' sh \
  ${ENTRY[@]+"${ENTRY[@]}"} python3 "/gate-src/tests/e2e/gate/$TIER.py" \
  --out /tmp/boss-gate-output/results.json > "$WORK/out/gate.log" 2>&1
status=$?
# A Colima guest may not share the Mac's temporary directory. Collect from the stopped
# container before cleanup just as on a remote daemon.
docker cp "$GATE:/tmp/boss-gate-output/." "$WORK/out" || status=1
if [ "$status" -ne 0 ] || [ ! -s "$WORK/out/results.json" ]; then
  tail -30 "$WORK/out/gate.log"
fi

python3 - "$WORK/out/results.json" "$started" <<'PY'
import json, sys, time
path, started = sys.argv[1], int(sys.argv[2])
try:
    data = json.load(open(path))
except (OSError, ValueError):
    print("e2e-gate: the gate wrote no results (see its log)")
    sys.exit(1)
rows = data["results"]
w = max(len(r["check"]) for r in rows) if rows else 10
print(f"\n{'check':<{w}}  bug   result   secs  detail")
print(f"{'-' * w}  ----  ------  -----  ------")
for r in rows:
    detail = r.get("detail", "")[:300 if r["status"] == "FAIL" else 140]
    print(f"{r['check']:<{w}}  {r.get('bug') or '':<4}  {r['status']:<6}  {r['seconds']:>5}  {detail}")
failed = sum(r["status"] == "FAIL" for r in rows)
skipped = sum(r["status"] == "SKIP" for r in rows)
verdict = "PASS" if rows and all(r["status"] == "PASS" for r in rows) else "FAIL"
print(f"\n{verdict}: {len(rows)} checks, {failed} failed, {skipped} not run; "
      f"{int(time.time()) - started}s in all")
sys.exit(0 if verdict == "PASS" else 1)
PY
report_status=$?

if [ "$KEEP" = 1 ]; then cp "$WORK/out/gate.log" "$WORK/out/gate.log.kept" 2>/dev/null || true; fi
exit $(( status == 0 && report_status == 0 ? 0 : 1 ))
