#!/bin/sh
set -eu

ca_file="${BOSS_BUILD_CA_FILE:-/run/secrets/boss_build_ca}"
if [ -s "$ca_file" ]; then
    export NODE_EXTRA_CA_CERTS="$ca_file"
fi
exec "$@"
