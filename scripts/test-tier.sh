#!/usr/bin/env bash
# Run one test tier (docs/operations/TEST_TIERS.md).
#
#   scripts/test-tier.sh P0                  must-never-break, <= 10 min
#   scripts/test-tier.sh P1                  main features + reported regressions, <= 30 min
#   scripts/test-tier.sh major               P0 then P1 (a major release's gate)
#   scripts/test-tier.sh changed [--since R] P0 + the tests of what changed since R (a minor's gate)
#   scripts/test-tier.sh P2                  everything not in tests/tiers.yaml (on demand / nightly)
#
# Red is not normal (BOSSAPP-1429, scripts/known_failures.py): a failure listed in the suite's
# baseline (tests/known-failures/<suite>.txt) is reported, not counted; any other failure fails the
# gate, so does a listed test that now passes (delete its line: the list only shrinks), and so does
# a red SECURITY GUARD (tests/tiers.yaml `security:`), listed or not. Every file a baseline names
# runs in every tier, so every line is judged on every run. The worker suite is small and always
# runs whole. The API tests spend ~6 s each resetting their database, so they run sharded across
# boss_test_t1..tN (created when missing) on the boss-community-db-1 container.
set -uo pipefail

TIER="${1:-}"; shift || true
SINCE="origin/main"
while [ $# -gt 0 ]; do
  case "$1" in
    --since) SINCE="${2:?--since needs a ref}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$TIER" in P0|P1|P2|major|changed) ;; *)
  echo "usage: scripts/test-tier.sh <P0|P1|P2|major|changed> [--since <ref>]" >&2; exit 2 ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
OUT="${TEST_TIER_OUT:-$(mktemp -d -t boss-tier-XXXX)}"
# A reused TEST_TIER_OUT must not judge an earlier run's logs.
rm -f "$OUT"/*.log "$OUT"/*.files "$OUT"/api.shard* "$OUT"/failed.txt "$OUT"/passed.txt \
  "$OUT"/ran.txt "$OUT"/unfinished.txt "$OUT"/judge.txt 2>/dev/null
DBC="${BOSS_TEST_DB_CONTAINER:-boss-community-db-1}"
# The test databases: <prefix> for the engine suite, <prefix>_t1..tN for the API shards.
DBP="${TEST_TIER_DB_PREFIX:-boss_test}"
case "$DBP" in boss_test*) ;; *) echo "TEST_TIER_DB_PREFIX must start with boss_test" >&2; exit 2 ;; esac

# ---- which files --------------------------------------------------------------------------
python3 - "$TIER" "$SINCE" "$OUT" <<'PY'
import os, re, subprocess, sys
import yaml

tier, since, out = sys.argv[1], sys.argv[2], sys.argv[3]
tiers = yaml.safe_load(open("tests/tiers.yaml"))
SUITES = {"engine": ("engine/tests", ".py"), "api": ("apps/api/tests", ".py"),
          "worker": ("apps/worker/tests", ".py"), "scripts": ("scripts/tests", ".py"),
          "web": ("apps/web/tests", (".ts", ".tsx"))}

def listed(t):
    return {s: list(tiers.get(t, {}).get(s) or []) for s in SUITES}

def every(s):
    d, ext = SUITES[s]
    return sorted(os.path.join(d, f) for f in os.listdir(d)
                  if f.endswith(ext) and (s == "web" or f.startswith("test_"))
                  and (s != "web" or ".test." in f))

if tier in ("P0", "P1"):
    pick = listed(tier)
elif tier == "major":
    p0, p1 = listed("P0"), listed("P1")
    pick = {s: p0[s] + [f for f in p1[s] if f not in p0[s]] for s in SUITES}
elif tier == "P2":
    known = {f for t in ("P0", "P1") for s in SUITES for f in listed(t)[s]}
    pick = {s: [f for f in every(s) if f not in known] for s in SUITES}
else:  # changed: P0 + tests that touch what changed since `since`
    pick = listed("P0")
    try:
        changed = subprocess.run(["git", "diff", "--name-only", f"{since}...HEAD"],
                                 capture_output=True, text=True, check=True).stdout.split()
    except subprocess.CalledProcessError:
        changed = subprocess.run(["git", "diff", "--name-only", since],
                                 capture_output=True, text=True).stdout.split()
    # Names a test would mention: a module's basename (python import or TS import path).
    stems = set()
    for f in changed:
        base = os.path.splitext(os.path.basename(f))[0]
        if base.startswith(("test_", "__")) or ".test" in base or len(base) < 4:
            continue
        stems.add(base)
    for s in SUITES:
        for f in every(s):
            if f in pick[s]:
                continue
            if f in changed:
                pick[s].append(f); continue
            try:
                text = open(f, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            if any(re.search(rf"\b{re.escape(st)}\b", text) for st in stems):
                pick[s].append(f)
# A file runs in the suite its PATH names (tiers.yaml lists scripts/tests under engine:).
sys.path.insert(0, "scripts")
import known_failures as kf

every_file = [f for s in SUITES for f in pick[s]]
by_path = {s: [] for s in SUITES}
for f in every_file:
    s = kf.suite_of(f) or "engine"
    if f not in by_path[s]:
        by_path[s].append(f)
# The worker suite is small: it always runs whole (it never ran anywhere before BOSSAPP-1429).
by_path["worker"] += [f for f in every("worker") if f not in by_path["worker"]]
# Every file a baseline names runs, so a line that passes now is caught as STALE.
try:
    listed_files = sorted({e.file for b in kf.load_all().values() for e in b.values()})
except ValueError as error:
    sys.exit(f"tests/known-failures: {error}")
# Every security guard runs in every tier: a red one fails every gate, so it must be in every run.
try:
    guard_files = sorted(kf.security_files())
except ValueError as error:
    sys.exit(f"tests/tiers.yaml: {error}")
for f in listed_files + guard_files:
    s = kf.suite_of(f)
    if s and f not in by_path[s]:
        by_path[s].append(f)
for s in SUITES:
    files = [f for f in by_path[s] if os.path.exists(f)]
    open(os.path.join(out, f"{s}.files"), "w").write("\n".join(files) + ("\n" if files else ""))
    print(f"{tier}: {s} {len(files)} files")
PY
[ $? -eq 0 ] || exit 2

# Only some suites (CI's p0 job runs engine,api,worker,scripts; its web job runs the whole web
# suite through scripts/web-unit-ci.py).
SUITES_ON="${TEST_TIER_SUITES:-engine,api,worker,scripts,web}"
for s in engine api worker scripts web; do
  case ",$SUITES_ON," in *",$s,"*) ;; *) : > "$OUT/$s.files" ;; esac
done

# ---- database ------------------------------------------------------------------------------
CORES=$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)
N=$(( CORES / 2 )); [ "$N" -gt 6 ] && N=6; [ "$N" -lt 1 ] && N=1
N="${TEST_TIER_SHARDS:-$N}"
if [ -n "${BOSS_TEST_PG_URL:-}" ]; then
  # A plain Postgres (CI's service container): BOSS_TEST_PG_URL=postgresql://user:pass@host:port
  # with the right to create databases. boss_test and boss_test_t1..tN are created when missing.
  eval "$(python3 - "$N" <<'PY'
import os, shlex, sys, urllib.parse
import psycopg
u = urllib.parse.urlsplit(os.environ["BOSS_TEST_PG_URL"])
n = int(sys.argv[1])
with psycopg.connect(f"{u.scheme}://{u.netloc}/postgres", autocommit=True) as c:
    have = {r[0] for r in c.execute("select datname from pg_database")}
    p = os.environ.get("TEST_TIER_DB_PREFIX", "boss_test")
    for name in [p] + [f"{p}_t{i}" for i in range(1, n + 1)]:
        if name not in have:
            c.execute(f'CREATE DATABASE "{name}"')
pw = urllib.parse.quote(urllib.parse.unquote(u.password or ""), safe="")
for k, v in (("PGU", u.username or ""), ("PW", pw), ("PGHOSTNAME", u.hostname or "localhost"),
             ("APISCHEME", "postgresql+psycopg"),
             ("PORT", str(u.port or 5432))):
    print(f"{k}={shlex.quote(v)}")
PY
)" || { echo "BOSS_TEST_PG_URL: cannot reach Postgres or create the test databases" >&2; exit 2; }
else
  PGU=$(docker exec "$DBC" printenv POSTGRES_USER 2>/dev/null) || { echo "no $DBC container" >&2; exit 2; }
  PGP=$(docker exec "$DBC" printenv POSTGRES_PASSWORD)
  PORT=$(docker port "$DBC" 5432 | head -1 | awk -F: '{print $NF}')
  PW=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$PGP")
  PGHOSTNAME=127.0.0.1
  APISCHEME=postgresql
  for i in $(seq 1 "$N"); do
    docker exec "$DBC" psql -U "$PGU" -d postgres -tAc "select 1 from pg_database where datname='${DBP}_t$i'" | grep -q 1 \
      || docker exec "$DBC" psql -U "$PGU" -d postgres -qc "CREATE DATABASE ${DBP}_t$i" >/dev/null
  done
fi

# Each pytest run is killed after this many seconds (a hung run is then a failure, not a hung gate).
RUN_TIMEOUT="${TEST_TIER_RUN_TIMEOUT:-1500}"
secs() { date +%s; }
summary=()

# ---- engine (one database: its fixtures expect the seeded boss_test) -------------------------
if [ -s "$OUT/engine.files" ]; then
  t0=$(secs)
  # DATABASE_URL too: a test that falls back to the default engine (ensure_schema) must reach the
  # same test database, never whatever answers on localhost:5432 on this machine.
  BOSS_TEST_DATABASE_URL="postgresql+psycopg://$PGU:$PW@$PGHOSTNAME:$PORT/$DBP" \
  DATABASE_URL="postgresql+psycopg://$PGU:$PW@$PGHOSTNAME:$PORT/$DBP" PYTHONPATH=.:engine \
    perl -e 'alarm shift; exec @ARGV' "$RUN_TIMEOUT" python3 -m pytest -q -p no:cacheprovider -W ignore $(cat "$OUT/engine.files") -rA > "$OUT/engine.log" 2>&1
  summary+=("engine $(( $(secs) - t0 ))s: $(tail -1 "$OUT/engine.log")")
fi

# ---- worker and scripts (no database; each its own run) --------------------------------------
for s in worker scripts; do
  [ -s "$OUT/$s.files" ] || continue
  t0=$(secs)
  PYTHONPATH=.:engine perl -e 'alarm shift; exec @ARGV' "$RUN_TIMEOUT" \
    python3 -m pytest -q -p no:cacheprovider -W ignore $(cat "$OUT/$s.files") -rA > "$OUT/$s.log" 2>&1
  summary+=("$s $(( $(secs) - t0 ))s: $(tail -1 "$OUT/$s.log")")
done

# ---- api (sharded) and web (in parallel with it) ---------------------------------------------
t0=$(secs)
if [ -s "$OUT/api.files" ]; then
  awk -v n="$N" '{print > ("'"$OUT"'/api.shard" (NR % n))}' "$OUT/api.files"
  for i in $(seq 0 $(( N - 1 ))); do
    [ -s "$OUT/api.shard$i" ] || continue
    ( DATABASE_URL="$APISCHEME://$PGU:$PW@$PGHOSTNAME:$PORT/${DBP}_t$(( i + 1 ))" PYTHONPATH=.:engine \
        perl -e 'alarm shift; exec @ARGV' "$RUN_TIMEOUT" python3 -m pytest -q -p no:cacheprovider -W ignore $(cat "$OUT/api.shard$i") -rA > "$OUT/api$i.log" 2>&1 ) &
  done
fi
if [ -s "$OUT/web.files" ]; then
  tw=$(secs)
  ( cd apps/web && npx vitest run --reporter=dot $(sed 's#^apps/web/##' "$OUT/web.files") > "$OUT/web.log" 2>&1
    echo "web_secs $(( $(date +%s) - tw ))" >> "$OUT/web.log" ) &
fi
wait
if [ -s "$OUT/api.files" ]; then
  summary+=("api $(( $(secs) - t0 ))s ($N shards): $(cat "$OUT"/api[0-9]*.log 2>/dev/null | grep -E '^[0-9]+ (passed|failed)' | sed 's/ in .*//' | tr '\n' ';')")
fi
if [ -s "$OUT/web.files" ]; then
  summary+=("web $(grep web_secs "$OUT/web.log" | awk '{print $2}')s: $(grep -E 'Tests +[0-9]' "$OUT/web.log" | tail -1 | sed 's/^ *//')")
fi

# ---- the baselines judge the run (scripts/known_failures.py) ----------------------------------
# Only real test ids (a log line such as "ERROR boss.api:main.py:1728" is not a failed test).
PYLOGS=$(ls "$OUT"/engine.log "$OUT"/worker.log "$OUT"/scripts.log "$OUT"/api[0-9]*.log 2>/dev/null)
cat $PYLOGS /dev/null | grep -E "^(FAILED|ERROR) " | sed 's/ - .*//' | awk '{print $2}' \
  | grep -E '^(engine|apps|scripts)/' > "$OUT/failed.txt"
grep -E "^ FAIL " "$OUT/web.log" 2>/dev/null | sed -E 's/^ FAIL  *//; s/ (>|\[).*//' | sed 's#^#apps/web/#' >> "$OUT/failed.txt"
# What ran: every file of every suite that ran. A run that never printed its summary ("N passed
# in 12.3s") crashed or was killed: that is a failure too, never a silent pass, and its files are
# "unfinished" (their baseline lines cannot be judged stale). "no tests ran" IS a summary, and it
# is a failure: an empty file on the tier must not pass the gate (BOSSAPP-972).
cat "$OUT"/engine.files "$OUT"/worker.files "$OUT"/scripts.files "$OUT"/api.files "$OUT"/web.files 2>/dev/null \
  | sort -u > "$OUT/ran.txt"
: > "$OUT/unfinished.txt"
for log in $PYLOGS; do
  name=$(basename "$log" .log)
  case "$name" in api[0-9]*) files="$OUT/api.shard${name#api}" ;; *) files="$OUT/$name.files" ;; esac
  if grep -qE 'no tests ran.* in [0-9.]+s' "$log"; then
    echo "$name: no tests ran (see $log)" >> "$OUT/failed.txt"
  fi
  # A collection error interrupts the whole session: nothing else in that run was judged.
  if grep -q 'Interrupted: ' "$log" || ! grep -qE '(passed|failed|error|skipped|no tests ran).* in [0-9.]+s' "$log"; then
    echo "$name: the pytest run did not finish (see $log)" >> "$OUT/failed.txt"
    cat "$files" >> "$OUT/unfinished.txt" 2>/dev/null
  fi
done
if [ -s "$OUT/web.files" ] && ! grep -qE 'Test Files ' "$OUT/web.log" 2>/dev/null; then
  echo "web: the vitest run did not finish (see $OUT/web.log)" >> "$OUT/failed.txt"
  cat "$OUT/web.files" >> "$OUT/unfinished.txt"
fi
sort -u -o "$OUT/failed.txt" "$OUT/failed.txt"
# What passed (pytest -rA lists every test's outcome): a baseline line is stale only when a test
# it names PASSED here, never because it was skipped. A web file that ran and did not fail passed.
cat $PYLOGS /dev/null | grep -E "^PASSED " | awk '{print $2}' > "$OUT/passed.txt"
if [ -s "$OUT/web.files" ]; then
  grep -vxF -f <(sed 's/::.*//' "$OUT/failed.txt") "$OUT/web.files" >> "$OUT/passed.txt" || true
fi
python3 scripts/known_failures.py judge --ran "$OUT/ran.txt" --failed "$OUT/failed.txt" \
  --passed "$OUT/passed.txt" --unfinished "$OUT/unfinished.txt" --suites "$SUITES_ON" \
  > "$OUT/judge.txt" 2>&1
JUDGED=$?
cat "$OUT/judge.txt"

echo "== tier $TIER  (logs: $OUT)"
for line in "${summary[@]}"; do echo "  $line"; done
[ "$JUDGED" -eq 0 ] && exit 0 || exit 1
