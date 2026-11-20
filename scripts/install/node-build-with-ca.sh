#!/bin/sh
set -eu

if [ -n "${BOSS_NPM_REGISTRY:-}" ]; then
    export npm_config_registry="$BOSS_NPM_REGISTRY"
fi

ca_file="${BOSS_BUILD_CA_FILE:-/run/secrets/boss_build_ca}"
if [ -s "$ca_file" ]; then
    export NODE_EXTRA_CA_CERTS="$ca_file"
fi
exec "$@"
