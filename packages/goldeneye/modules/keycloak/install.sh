#!/usr/bin/env bash
set -euo pipefail
KIT="${SHARED_SERVER_KIT_ROOT:-/opt/shared-server}"
# shellcheck source=/dev/null
source "$KIT/lib/common.sh"
ge_require_root
ge_step "optional/keycloak"
ge_info "Keycloak is a multi-service production stack."
ge_info "Kit marks the package installed and writes a compose skeleton."
RUN=/opt/shared-server/run/keycloak
mkdir -p "$RUN"
cat >"$RUN/compose.yaml" <<'YAML'
# Skeleton — production dogfood uses the full keycloak/ tree with Postgres + Traefik labels.
services:
  keycloak:
    image: quay.io/keycloak/keycloak:26.0
    command: ["start-dev"]
    environment:
      KEYCLOAK_ADMIN: admin
      KEYCLOAK_ADMIN_PASSWORD: change-me
    ports: ["127.0.0.1:8089:8080"]
    profiles: ["manual"]
YAML
ge_info "edit $RUN/compose.yaml then: docker compose --profile manual up -d"
