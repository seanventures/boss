#!/usr/bin/env bash
# Run one test tier (docs/operations/TEST_TIERS.md).
#
#   scripts/test-tier.sh P0                  must-never-break, <= 10 min
#   scripts/test-tier.sh P1                  main features + reported regressions, <= 30 min
#   scripts/test-tier.sh major               P0 then P1 (a major release's gate)
#   scripts/test-tier.sh changed [--since R] P0 + the tests of what changed since R (a minor's gate)
#   scripts/test-tier.sh P2                  everything not in tests/tiers.yaml (on demand / nightly)
#
# Only NEW failures fail the gate: a failure listed in tests/known-failures.txt is reported, not
# counted. The API tests spend ~6 s each resetting their database, so they run sharded across
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
DBC="${BOSS_TEST_DB_CONTAINER:-boss-community-db-1}"

# ---- which files --------------------------------------------------------------------------
python3 - "$TIER" "$SINCE" "$OUT" <<'PY'
import os, re, subprocess, sys
import yaml

tier, since, out = sys.argv[1], sys.argv[2], sys.argv[3]
tiers = yaml.safe_load(open("tests/tiers.yaml"))
SUITES = {"engine": ("engine/tests", ".py"), "api": ("apps/api/tests", ".py"),
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
for s in SUITES:
    files = [f for f in pick[s] if os.path.exists(f)]
    open(os.path.join(out, f"{s}.files"), "w").write("\n".join(files) + ("\n" if files else ""))
    print(f"{tier}: {s} {len(files)} files")
PY
[ $? -eq 0 ] || exit 2

# ---- database ------------------------------------------------------------------------------
PGU=$(docker exec "$DBC" printenv POSTGRES_USER 2>/dev/null) || { echo "no $DBC container" >&2; exit 2; }
PGP=$(docker exec "$DBC" printenv POSTGRES_PASSWORD)
PORT=$(docker port "$DBC" 5432 | head -1 | awk -F: '{print $NF}')
PW=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$PGP")
CORES=$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)
N=$(( CORES / 2 )); [ "$N" -gt 6 ] && N=6; [ "$N" -lt 1 ] && N=1
for i in $(seq 1 "$N"); do
  docker exec "$DBC" psql -U "$PGU" -d postgres -tAc "select 1 from pg_database where datname='boss_test_t$i'" | grep -q 1 \
    || docker exec "$DBC" psql -U "$PGU" -d postgres -qc "CREATE DATABASE boss_test_t$i" >/dev/null
done

secs() { date +%s; }
summary=()

# ---- engine (one database: its fixtures expect the seeded boss_test) -------------------------
if [ -s "$OUT/engine.files" ]; then
  t0=$(secs)
  BOSS_TEST_DATABASE_URL="postgresql+psycopg://$PGU:$PW@127.0.0.1:$PORT/boss_test" PYTHONPATH=.:engine \
    python3 -m pytest -q -p no:cacheprovider -W ignore $(cat "$OUT/engine.files") -rfE > "$OUT/engine.log" 2>&1
  summary+=("engine $(( $(secs) - t0 ))s: $(tail -1 "$OUT/engine.log")")
fi

# ---- api (sharded) and web (in parallel with it) ---------------------------------------------
t0=$(secs)
if [ -s "$OUT/api.files" ]; then
  awk -v n="$N" '{print > ("'"$OUT"'/api.shard" (NR % n))}' "$OUT/api.files"
  for i in $(seq 0 $(( N - 1 ))); do
    [ -s "$OUT/api.shard$i" ] || continue
    ( DATABASE_URL="postgresql://$PGU:$PW@localhost:$PORT/boss_test_t$(( i + 1 ))" PYTHONPATH=.:engine \
        python3 -m pytest -q -p no:cacheprovider -W ignore $(cat "$OUT/api.shard$i") -rfE > "$OUT/api$i.log" 2>&1 ) &
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

# ---- only NEW failures fail the gate ----------------------------------------------------------
# Only real test ids (a log line such as "ERROR boss.api:main.py:1728" is not a failed test).
cat "$OUT"/engine.log "$OUT"/api[0-9]*.log 2>/dev/null | grep -E "^(FAILED|ERROR) " | sed 's/ - .*//' | awk '{print $2}' \
  | grep -E '^(engine|apps)/' > "$OUT/failed.txt"
grep -E "^ FAIL " "$OUT/web.log" 2>/dev/null | sed 's/^ FAIL  *//; s/ >.*//' >> "$OUT/failed.txt"
sort -u -o "$OUT/failed.txt" "$OUT/failed.txt"
KNOWN="tests/known-failures.txt"
if [ -f "$KNOWN" ]; then grep -vE '^\s*(#|$)' "$KNOWN" | sort -u > "$OUT/known.txt"; else : > "$OUT/known.txt"; fi
# A known entry may name a whole file (prefix match) or one test id.
python3 - "$OUT" <<'PY'
import sys, os
out = sys.argv[1]
known = [l.strip() for l in open(os.path.join(out, "known.txt")) if l.strip()]
failed = [l.strip() for l in open(os.path.join(out, "failed.txt")) if l.strip()]
new = [f for f in failed if not any(f == k or f.startswith(k + "::") or f == k.split("::")[0] and "::" not in k for k in known)]
old = [f for f in failed if f not in new]
open(os.path.join(out, "new.txt"), "w").write("\n".join(new) + ("\n" if new else ""))
print(f"known failures (not counted): {len(old)}")
print(f"NEW failures: {len(new)}")
for f in new[:40]:
    print("  " + f)
PY

echo "== tier $TIER  (logs: $OUT)"
for line in "${summary[@]}"; do echo "  $line"; done
[ -s "$OUT/new.txt" ] && exit 1 || exit 0
