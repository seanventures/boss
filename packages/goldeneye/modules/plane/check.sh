#!/usr/bin/env bash
# Health/drift check for module plane — optional project tracker notes / stack.
set -euo pipefail
RUN="${SHARED_SERVER_PLANE_RUN:-/opt/shared-server/run/plane}"

if [ -d "$RUN" ]; then
  echo "check: plane run dir $RUN present"
  if [ -f "$RUN/README.md" ]; then
    echo "check: plane operator notes present"
  fi
else
  echo "check: plane note: not materialised (optional; dogfood often runs Plane out-of-band)"
fi

if command -v docker >/dev/null 2>&1; then
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -Eqi 'plane'; then
    echo "check: plane related container running"
  fi
fi

# Reachability of PLANE URL is environment-specific; do not hard-fail.
echo "check: plane ok"
exit 0
