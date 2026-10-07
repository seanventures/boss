#!/bin/sh
# A51 in a container (Linux): the image is the BOSS api image (Python deps present); the helper
# binary was compiled by build_helper.sh into /out. Runs as root only to set up what the
# Dockerfile does, then hands over to scripts/sandbox/entrypoint.sh exactly as compose runs it.
set -eu
export DEBIAN_FRONTEND=noninteractive
if ! command -v iptables >/dev/null 2>&1 || ! command -v setcap >/dev/null 2>&1; then
  apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends iptables libcap2-bin >/dev/null
fi
id boss >/dev/null 2>&1 || { addgroup --system boss >/dev/null; adduser --system --ingroup boss --home /home/boss boss >/dev/null; }
mkdir -p /usr/local/libexec/boss /var/lib/boss/identity /var/lib/boss/sandboxes /run/boss-shim
cp /out/boss-sandbox-exec /usr/local/libexec/boss/boss-sandbox-exec
chown root:boss /usr/local/libexec/boss/boss-sandbox-exec
chmod 0750 /usr/local/libexec/boss/boss-sandbox-exec
setcap cap_setuid,cap_setgid,cap_chown,cap_fowner+ep /usr/local/libexec/boss/boss-sandbox-exec
chown -R boss:boss /var/lib/boss /run/boss-shim
mkdir -p /app && chmod 0755 /app
export HOME=/home/boss
exec sh /src/scripts/sandbox/entrypoint.sh python3 /src/engine/tests/sandbox_a51/harness.py
