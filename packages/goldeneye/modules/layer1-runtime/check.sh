#!/usr/bin/env bash
# Health/drift check for module layer1-runtime — docker + external networks.
set -euo pipefail
fail=0

if ! command -v docker >/dev/null 2>&1; then
  echo "check: layer1-runtime FAIL docker not on PATH" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "check: layer1-runtime FAIL docker daemon not reachable" >&2
  exit 1
fi
echo "check: layer1-runtime docker ok"

for net in web storage ops; do
  if docker network inspect "$net" >/dev/null 2>&1; then
    echo "check: layer1-runtime network $net present"
  else
    echo "check: layer1-runtime WARN network $net missing" >&2
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "check: layer1-runtime FAIL" >&2
  exit 1
fi
echo "check: layer1-runtime ok"
exit 0
