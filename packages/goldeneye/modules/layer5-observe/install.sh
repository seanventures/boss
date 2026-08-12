#!/usr/bin/env bash
# layer5-observe — lightweight portal + log viewer placeholders
set -euo pipefail
KIT="${GOLDENEYE_KIT_ROOT:-/opt/goldeneye}"
# shellcheck source=/dev/null
source "$KIT/lib/common.sh"
ge_require_root
ge_step "layer5-observe"

command -v docker >/dev/null || ge_die "docker required"
docker network inspect web >/dev/null 2>&1 || docker network create web

RUN=/opt/goldeneye/run/observe
mkdir -p "$RUN"

# Dozzle — container log UI (bind localhost / tailnet only in real ops)
cat >"$RUN/compose.yaml" <<'YAML'
services:
  dozzle:
    image: amir20/dozzle:latest
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    ports:
      - "127.0.0.1:8088:8080"
    networks: [web]
networks:
  web:
    external: true
YAML
( cd "$RUN" && docker compose -p ge-observe up -d ) || ge_info "observe stack failed to start (non-fatal for kit)"

ge_info "layer5-observe complete (dozzle on 127.0.0.1:8088)"
ge_info "route admin UIs on tailnet hostnames only in production"
