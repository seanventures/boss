#!/bin/sh
# Compile scripts/sandbox/boss-sandbox-exec.c exactly as Dockerfile.community does, into /out.
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends gcc libc6-dev >/dev/null
gcc -O2 -Wall -Wextra -Werror -D_FORTIFY_SOURCE=2 -fstack-protector-strong -fPIE -pie \
    -o /out/boss-sandbox-exec /src/scripts/sandbox/boss-sandbox-exec.c
echo built
