#!/usr/bin/env bash
# Health/drift check for module layer2-edge — edge run dir / Traefik config.
set -euo pipefail
fail=0
RUN="${SHARED_SERVER_EDGE_RUN:-/opt/shared-server/run/edge}"

if [ -d "$RUN" ]; then
  echo "check: layer2-edge run dir $RUN present"
else
  echo "check: layer2-edge WARN missing $RUN" >&2
  fail=1
fi

if [ -f "$RUN/traefik/traefik.yml" ] || [ -f "$RUN/traefik/traefik.yaml" ]; then
  echo "check: layer2-edge traefik static config present"
else
  echo "check: layer2-edge WARN traefik config missing under $RUN/traefik" >&2
  # Only hard-fail when module is marked installed.
  if [ -f "${SHARED_SERVER_STATE_ROOT:-/var/lib/shared-server}/modules/layer2-edge.json" ]; then
    fail=1
  fi
fi

if command -v docker >/dev/null 2>&1; then
  if docker network inspect web >/dev/null 2>&1; then
    echo "check: layer2-edge network web present"
  else
    echo "check: layer2-edge WARN network web missing" >&2
    fail=1
  fi
fi

if [ "$fail" -ne 0 ]; then
  echo "check: layer2-edge FAIL" >&2
  exit 1
fi
echo "check: layer2-edge ok"
exit 0
