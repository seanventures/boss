#!/usr/bin/env bash
# Health/drift check for module layer0-host.
# Exit 0 healthy; non-zero when baseline artifacts are missing.
set -euo pipefail
fail=0

if [ -f /etc/sysctl.d/99-shared-server.conf ]; then
  echo "check: layer0-host sysctl fragment present"
else
  echo "check: layer0-host WARN missing /etc/sysctl.d/99-shared-server.conf" >&2
  # Soft when not yet installed on this host; still exit 0 if module state says installed
  # only when state file exists without fragment is a drift.
  if [ -f "${SHARED_SERVER_STATE_ROOT:-/var/lib/shared-server}/modules/layer0-host.json" ] \
     || [ -f "${SHARED_SERVER_STATE_ROOT:-/var/lib/shared-server}/packages/layer0-host.installed" ]; then
    fail=1
  fi
fi

if [ -f /etc/ssh/sshd_config.d/50-shared-server.conf ]; then
  echo "check: layer0-host sshd fragment present"
else
  echo "check: layer0-host note: sshd hardening fragment not present (optional on first check)"
fi

if [ "$fail" -ne 0 ]; then
  echo "check: layer0-host FAIL (drift)" >&2
  exit 1
fi
echo "check: layer0-host ok"
exit 0
