#!/usr/bin/env bash
# Health/drift check for module meilisearch — optional full-text search.
set -euo pipefail
RUN="${GOLDENEYE_MEILI_RUN:-/opt/goldeneye/run/meilisearch}"
fail=0

if [ -d "$RUN" ]; then
  echo "check: meilisearch run dir $RUN present"
else
  echo "check: meilisearch note: not materialised (optional module)"
fi

if command -v docker >/dev/null 2>&1; then
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -Eqi 'meili'; then
    echo "check: meilisearch container running"
  elif [ -f "$RUN/compose.yaml" ] || [ -f "$RUN/docker-compose.yml" ]; then
    echo "check: meilisearch WARN compose present but container not running" >&2
    fail=1
  fi
fi

if [ "$fail" -ne 0 ]; then
  echo "check: meilisearch FAIL" >&2
  exit 1
fi
echo "check: meilisearch ok"
exit 0
