#!/usr/bin/env bash
# layer2-edge — Traefik + registry (+ optional hubcache)
set -euo pipefail
KIT="${GOLDENEYE_KIT_ROOT:-/opt/goldeneye}"
# shellcheck source=/dev/null
source "$KIT/lib/common.sh"
ge_require_root
ge_step "layer2-edge"

command -v docker >/dev/null || ge_die "docker required"
docker network inspect web >/dev/null 2>&1 || docker network create web

PUBLIC_IP="$(ge_load_site_value network.public_ip)"
TN_IP="$(ge_load_site_value network.tailnet_ip)"
if [ -z "$TN_IP" ] && command -v tailscale >/dev/null 2>&1; then
  TN_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
fi
ACME_EMAIL="$(ge_load_site_value tls.acme_email)"
ACME_EMAIL="${ACME_EMAIL:-ops@example.com}"
RUN=/opt/goldeneye/run/edge
mkdir -p "$RUN/traefik" "$RUN/registry" "$RUN/letsencrypt"
chmod 700 "$RUN/letsencrypt"
touch "$RUN/letsencrypt/acme.json"
chmod 600 "$RUN/letsencrypt/acme.json"

# --- Traefik static config ---
cat >"$RUN/traefik/traefik.yml" <<YAML
api:
  dashboard: true
  insecure: true
entryPoints:
  web:
    address: ":80"
    http:
      redirections:
        entryPoint:
          to: websecure
          scheme: https
  websecure:
    address: ":443"
providers:
  docker:
    endpoint: "unix:///var/run/docker.sock"
    exposedByDefault: false
    network: web
certificatesResolvers:
  le:
    acme:
      email: ${ACME_EMAIL}
      storage: /letsencrypt/acme.json
      httpChallenge:
        entryPoint: web
YAML

cat >"$RUN/traefik/compose.yaml" <<'YAML'
services:
  traefik:
    image: traefik:v3.3
    restart: unless-stopped
    network_mode: host
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./traefik.yml:/traefik.yml:ro
      - ../letsencrypt:/letsencrypt
    command:
      - --configFile=/traefik.yml
YAML

# Bind note: host network so PUBLIC_IP/TN_IP binding can be refined later.
( cd "$RUN/traefik" && docker compose -p ge-traefik up -d )

# --- Registry ---
cat >"$RUN/registry/compose.yaml" <<'YAML'
services:
  registry:
    image: registry:2
    restart: unless-stopped
    networks: [web]
    ports:
      - "5000:5000"
    volumes:
      - registry-data:/var/lib/registry
volumes:
  registry-data:
networks:
  web:
    external: true
YAML
( cd "$RUN/registry" && docker compose -p ge-registry up -d )

if [ "$(ge_load_site_value features.enable_hubcache)" = "true" ] || [ -z "$(ge_load_site_value features.enable_hubcache)" ]; then
  # lightweight pull-through optional — skip if registry port conflicts
  ge_info "hubcache: enable later via optional package if needed"
fi

ge_info "layer2-edge complete (Traefik + registry:5000)"
ge_info "point public DNS A records at ${PUBLIC_IP:-(set network.public_ip)}"
