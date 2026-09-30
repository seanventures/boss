#!/usr/bin/env bash
# Health/drift check for module minio — compose project or container.
set -euo pipefail
RUN="${SHARED_SERVER_MINIO_RUN:-/opt/shared-server/run/minio}"
fail=0

if [ -f "$RUN/compose.yaml" ] || [ -f "$RUN/docker-compose.yml" ]; then
  echo "check: minio compose present at $RUN"
else
  echo "check: minio note: no compose at $RUN (may not be installed)"
fi

if command -v docker >/dev/null 2>&1; then
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -Eq 'minio|ge-minio'; then
    echo "check: minio container running"
  elif [ -f "$RUN/compose.yaml" ]; then
    echo "check: minio WARN compose present but no running container" >&2
    fail=1
  fi
fi

if [ "$fail" -ne 0 ]; then
  echo "check: minio FAIL" >&2
  exit 1
fi
echo "check: minio ok"
exit 0
