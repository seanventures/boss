#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROFILE="m6"
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="${2:?missing value for --profile}"
      shift 2
      ;;
    --profile=*)
      PROFILE="${1#*=}"
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: scripts/run-module-e2e.sh [--profile m6|m2|m3|m4|m5|m7] [extra pytest args...]

Profiles:
  m6  Baseline-ready opaque-box Tier 1-4 E2E suite (0 skips / 0 xfails enforced)
  m2  m6 + Agent Framework M2 acceptance deltas
  m3  m6 + MetaTron CLI M3 acceptance deltas
  m4  m6 + P1 Hardening M4 acceptance deltas
  m5  m6 + P1 Features M5 acceptance deltas
  m7  Full combined suite (m6 + m2 + m3 + m4 + m5)
EOF
      exit 0
      ;;
    --)
      shift
      EXTRA_ARGS+=("$@")
      break
      ;;
    *)
      EXTRA_ARGS+=("$1")
      shift
      ;;
  esac
done

PYTHON_BIN="python3"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
fi

export BOSS_DB_PASSWORD="${BOSS_DB_PASSWORD:-boss_local_test_pw}"
export BOSS_TEST_PG_URL="${BOSS_TEST_PG_URL:-postgresql://boss:${BOSS_DB_PASSWORD}@127.0.0.1:55700}"
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export PYTHONPATH="$ROOT:$ROOT/engine:$ROOT/apps/worker${PYTHONPATH:+:$PYTHONPATH}"

BASE_FILES=(
  "tests/e2e/module_suite/test_mt_baseline_t1_t2.py"
  "tests/e2e/module_suite/test_af_baseline_t1_t2.py"
  "tests/e2e/module_suite/test_p1_baseline_t1_t2.py"
  "tests/e2e/module_suite/test_cross_module_t3_t4.py"
)

TARGET_FILES=("${BASE_FILES[@]}")
case "$PROFILE" in
  m6) ;;
  m2) TARGET_FILES+=("tests/e2e/module_suite/deltas/test_m2_af_acceptance.py") ;;
  m3) TARGET_FILES+=("tests/e2e/module_suite/deltas/test_m3_metatron_acceptance.py") ;;
  m4) TARGET_FILES+=("tests/e2e/module_suite/deltas/test_m4_p1_hardening_acceptance.py") ;;
  m5) TARGET_FILES+=("tests/e2e/module_suite/deltas/test_m5_p1_features_acceptance.py") ;;
  m7)
    TARGET_FILES+=(
      "tests/e2e/module_suite/deltas/test_m2_af_acceptance.py"
      "tests/e2e/module_suite/deltas/test_m3_metatron_acceptance.py"
      "tests/e2e/module_suite/deltas/test_m4_p1_hardening_acceptance.py"
      "tests/e2e/module_suite/deltas/test_m5_p1_features_acceptance.py"
    )
    ;;
  *)
    echo "Unknown profile: $PROFILE (expected m6|m2|m3|m4|m5|m7)" >&2
    exit 2
    ;;
esac

set +e
OUTPUT="$("$PYTHON_BIN" -m pytest \
  --strict-markers \
  -p no:cacheprovider \
  -q \
  --tb=short \
  "${TARGET_FILES[@]}" \
  ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} \
  2>&1)"
PYTEST_RC=$?
set -e

printf '%s\n' "$OUTPUT"

if [[ $PYTEST_RC -ne 0 ]]; then
  echo "[run-module-e2e] pytest failed with exit code $PYTEST_RC for profile $PROFILE" >&2
  exit "$PYTEST_RC"
fi

if [[ "$PROFILE" == "m6" ]]; then
  if printf '%s\n' "$OUTPUT" | grep -E '[0-9]+ (skipped|xfailed|xpassed)' >/dev/null 2>&1; then
    echo "[run-module-e2e] REFUSED: profile $PROFILE had skipped/xfailed tests (zero-skip invariant violated)" >&2
    exit 2
  fi
  echo "[run-module-e2e] PASS profile=$PROFILE (0 failed, 0 skipped, 0 xfailed)"
else
  echo "[run-module-e2e] PASS profile=$PROFILE (0 failed)"
fi
