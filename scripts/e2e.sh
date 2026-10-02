#!/usr/bin/env bash
# The end-to-end harness, in one command. See tests/e2e/README.md.
#
#   scripts/e2e.sh                 boot a disposable stack, drive every template and example,
#                                  walk the console, tear it down; exit non-zero on any failure
#   scripts/e2e.sh up              boot it and leave it up (then: workflows, console, down)
#   scripts/e2e.sh selftest        the harness's own fast tests (no Docker, no browser)
#   scripts/e2e.sh all --target live --real-models
#                                  the same checks against an installed BOSS (boss-1)
#
# Never touches the operator's install: the local stack is its own Postgres container and a
# database named boss_e2e, and a live target is only ever named through BOSS_E2E_* variables.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON_BIN:-python3}"
if [[ -x "$ROOT/.venv/bin/python" && -z "${PYTHON_BIN:-}" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
fi

if [[ "${1:-}" == "selftest" ]]; then
  shift
  cd "$ROOT"
  PYTHONPATH="$ROOT:$ROOT/engine" exec "$PYTHON" -m pytest -q -p no:warnings tests/e2e/selftest "$@"
fi

cd "$ROOT"
PYTHONPATH="$ROOT:$ROOT/engine" exec "$PYTHON" tests/e2e/run.py "$@"
