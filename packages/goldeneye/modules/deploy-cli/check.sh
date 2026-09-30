#!/usr/bin/env bash
# Health/drift check for module deploy-cli — ge/deploy on PATH.
set -euo pipefail
fail=0

if command -v ge >/dev/null 2>&1; then
  echo "check: deploy-cli ge → $(command -v ge)"
  ge version >/dev/null 2>&1 || ge help >/dev/null 2>&1 || true
else
  # Fall back to kit path
  KIT="${SHARED_SERVER_KIT_ROOT:-/opt/shared-server}"
  if [ -x "$KIT/bin/ge" ] || [ -x "$KIT/core/bin/ge" ]; then
    echo "check: deploy-cli ge present under kit ($KIT) but not on PATH"
  else
    echo "check: deploy-cli FAIL ge not found on PATH or kit" >&2
    fail=1
  fi
fi

if command -v deploy >/dev/null 2>&1; then
  echo "check: deploy-cli deploy → $(command -v deploy)"
else
  echo "check: deploy-cli note: deploy wrapper not on PATH (ge deploy still works)"
fi

if [ "$fail" -ne 0 ]; then
  echo "check: deploy-cli FAIL" >&2
  exit 1
fi
echo "check: deploy-cli ok"
exit 0
