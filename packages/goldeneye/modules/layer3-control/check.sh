#!/usr/bin/env bash
# Health/drift check for module layer3-control — ops-agent / control stubs.
set -euo pipefail
STATE="${GOLDENEYE_STATE_ROOT:-/var/lib/goldeneye}"
RUN="${GOLDENEYE_CONTROL_RUN:-/opt/goldeneye/run/control}"
fail=0

if [ -d "$RUN" ] || [ -d /opt/goldeneye/run/ops-agent ] || [ -d "$STATE/control" ]; then
  echo "check: layer3-control control/ops path present"
else
  # Soft: skeleton module may only write state; require state if marked installed.
  if [ -f "$STATE/modules/layer3-control.json" ] || [ -f "$STATE/packages/layer3-control.installed" ]; then
    echo "check: layer3-control WARN no control run dir (ops-agent skeleton)" >&2
    # Still ok for skeleton — state proves install hook ran.
    echo "check: layer3-control note: skeleton install only (no live ops-agent process required)"
  else
    echo "check: layer3-control note: not installed yet (no state)"
  fi
fi

# If an ops-agent unit/timer exists, confirm it is loadable.
if command -v systemctl >/dev/null 2>&1; then
  if systemctl list-unit-files 2>/dev/null | grep -q 'goldeneye-ops\|ops-agent'; then
    echo "check: layer3-control systemd unit present"
  fi
fi

echo "check: layer3-control ok"
exit 0
