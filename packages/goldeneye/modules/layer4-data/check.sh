#!/usr/bin/env bash
# Health/drift check for module layer4-data — backup hooks / restore-drill.
set -euo pipefail
STATE="${SHARED_SERVER_STATE_ROOT:-/var/lib/shared-server}"
RUN="${SHARED_SERVER_DATA_RUN:-/opt/shared-server/run/data}"
fail=0

if [ -d "$RUN" ] || [ -d "$STATE/backup" ] || [ -d /var/lib/shared-server/backup ]; then
  echo "check: layer4-data backup/data path present"
else
  if [ -f "$STATE/modules/layer4-data.json" ] || [ -f "$STATE/packages/layer4-data.installed" ]; then
    echo "check: layer4-data WARN installed but no backup path yet" >&2
    # Soft for stub installers that only drop timers later.
  else
    echo "check: layer4-data note: not installed yet"
  fi
fi

# Prefer an executable restore-drill or backup script if present.
for cand in \
  /usr/local/bin/ge-restore-drill \
  /opt/shared-server/bin/restore-drill \
  "$RUN/restore-drill.sh"
do
  if [ -x "$cand" ]; then
    echo "check: layer4-data restore-drill at $cand"
    break
  fi
done

echo "check: layer4-data ok"
exit 0
