#!/usr/bin/env bash
set -euo pipefail
KIT="${SHARED_SERVER_KIT_ROOT:-/opt/shared-server}"
# shellcheck source=/dev/null
source "$KIT/lib/common.sh"
ge_require_root
ge_step "optional/meilisearch"
docker network inspect web >/dev/null 2>&1 || docker network create web
RUN=/opt/shared-server/run/meilisearch
mkdir -p "$RUN/data"
MASTER="${MEILI_MASTER_KEY:-$(openssl rand -hex 24)}"
cat >"$RUN/compose.yaml" <<YAML
services:
  meilisearch:
    image: getmeili/meilisearch:v1.11
    restart: unless-stopped
    environment:
      MEILI_MASTER_KEY: ${MASTER}
      MEILI_ENV: production
    volumes: ["./data:/meili_data"]
    ports: ["127.0.0.1:7700:7700"]
    networks: [web]
networks:
  web: { external: true }
YAML
( cd "$RUN" && docker compose -p ge-meili up -d )
echo "$MASTER" >/var/lib/shared-server/meili-master-key
chmod 600 /var/lib/shared-server/meili-master-key
ge_info "meilisearch on 127.0.0.1:7700; master key in /var/lib/shared-server/meili-master-key"
