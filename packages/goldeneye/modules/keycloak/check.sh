#!/usr/bin/env bash
# Health/drift check for module keycloak — optional OIDC realm factory.
set -euo pipefail
RUN="${SHARED_SERVER_KEYCLOAK_RUN:-/opt/shared-server/run/keycloak}"
fail=0

if [ -d "$RUN" ]; then
  echo "check: keycloak run dir $RUN present"
else
  echo "check: keycloak note: not materialised (optional module)"
fi

if command -v docker >/dev/null 2>&1; then
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -Eqi 'keycloak'; then
    echo "check: keycloak container running"
  elif [ -f "$RUN/compose.yaml" ] || [ -f "$RUN/docker-compose.yml" ]; then
    echo "check: keycloak WARN compose present but container not running" >&2
    fail=1
  fi
fi

if [ "$fail" -ne 0 ]; then
  echo "check: keycloak FAIL" >&2
  exit 1
fi
echo "check: keycloak ok"
exit 0
