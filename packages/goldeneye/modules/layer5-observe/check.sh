#!/usr/bin/env bash
# Health/drift check for module layer5-observe — portal / log viewer placeholders.
set -euo pipefail
RUN="${GOLDENEYE_OBSERVE_RUN:-/opt/goldeneye/run/observe}"
STATE="${GOLDENEYE_STATE_ROOT:-/var/lib/goldeneye}"

if [ -d "$RUN" ] || [ -d /opt/goldeneye/run/homepage ] || [ -d /opt/goldeneye/run/dozzle ]; then
  echo "check: layer5-observe run path present"
else
  if [ -f "$STATE/modules/layer5-observe.json" ] || [ -f "$STATE/packages/layer5-observe.installed" ]; then
    echo "check: layer5-observe note: installed; portal paths not materialised (placeholder ok)"
  else
    echo "check: layer5-observe note: not installed yet"
  fi
fi

if command -v docker >/dev/null 2>&1; then
  # Optional containers — report, do not fail if absent.
  for name in homepage dozzle uptime-kuma ge-observe; do
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$name"; then
      echo "check: layer5-observe container $name running"
    fi
  done
fi

echo "check: layer5-observe ok"
exit 0
