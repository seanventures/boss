#!/usr/bin/env bash
set -euo pipefail
KIT="${GOLDENEYE_KIT_ROOT:-/opt/goldeneye}"
# shellcheck source=/dev/null
source "$KIT/lib/common.sh"
ge_require_root
ge_step "optional/minio"
docker network inspect storage >/dev/null 2>&1 || docker network create storage
RUN=/opt/goldeneye/run/minio
mkdir -p "$RUN/data"
ROOT_USER="${MINIO_ROOT_USER:-goldeneye}"
ROOT_PASS="${MINIO_ROOT_PASSWORD:-$(openssl rand -hex 16)}"
cat >"$RUN/compose.yaml" <<YAML
services:
  minio:
    image: minio/minio:latest
    command: server /data --console-address ":9001"
    restart: unless-stopped
    environment:
      MINIO_ROOT_USER: ${ROOT_USER}
      MINIO_ROOT_PASSWORD: ${ROOT_PASS}
    volumes: ["./data:/data"]
    ports: ["127.0.0.1:9000:9000", "127.0.0.1:9001:9001"]
    networks: [storage, web]
networks:
  storage: { external: true }
  web: { external: true }
YAML
( cd "$RUN" && docker compose -p ge-minio up -d )
echo "$ROOT_PASS" >/var/lib/goldeneye/minio-root-password
chmod 600 /var/lib/goldeneye/minio-root-password
ge_info "minio up (console 127.0.0.1:9001); root password in /var/lib/goldeneye/minio-root-password"
