#!/usr/bin/env bash
# The end-to-end release gate (docs/operations/TEST_TIERS.md, "Before a release").
#
#   scripts/e2e-gate.sh agent [--brief <name>] [--with-connector] [--budget-min N] [--keep]
#       Tier B: ONE real, model-driven build of a known-good brief (tests/e2e/briefs/), started
#       on the local install as the test owner through BOSS's API, watched to an opened PR,
#       asserted, timed, and cleaned up after a PASS (scripts/e2e_gate_agent.py).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tier="${1:-}"
case "$tier" in
  agent)
    shift
    exec python3 "$here/e2e_gate_agent.py" "$@"
    ;;
  ""|-h|--help)
    sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'
    [ -n "$tier" ] || exit 2
    ;;
  *)
    echo "e2e-gate: unknown tier '$tier' (have: agent)" >&2
    exit 2
    ;;
esac
