#!/usr/bin/env bash
set -euo pipefail
KIT="${GOLDENEYE_KIT_ROOT:-/opt/goldeneye}"
# shellcheck source=/dev/null
source "$KIT/lib/common.sh"
ge_require_root
ge_step "optional/plane"
RUN=/opt/goldeneye/run/plane
mkdir -p "$RUN"
cat >"$RUN/README.md" <<'MD'
# Plane — project tracking (system of record)

Production dogfood runs Plane on the control node with Traefik + Postgres.
Use the official self-host compose, then point BOSS at:
  PLANE_API_KEY, workspace, project id

BOSS treats Plane as the default work tracker (see WORK_TRACKER_PROVIDERS.md).
MD
ge_info "plane notes at $RUN/README.md"
