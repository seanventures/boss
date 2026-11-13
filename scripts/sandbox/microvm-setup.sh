#!/usr/bin/env bash
# microvm-setup.sh — bake the two guest artifacts the Firecracker backend needs:
#
#   <artifacts>/vmlinux       an uncompressed x86_64 kernel (Firecracker CI build)
#   <artifacts>/rootfs.ext4   Alpine + python3/git/node + the in-guest agent at
#                             /opt/boss/in_guest_agent.py + /sbin/boss-init
#
# Runs UNPRIVILEGED on a Linux x86_64 host with docker + e2fsprogs: the rootfs
# is populated from a `docker export` and packed with `mke2fs -d`, so nothing
# is ever loop-mounted and no sudo is needed.
#
# Usage: scripts/sandbox/microvm-setup.sh [artifacts-dir]
#   default artifacts dir: ~/.boss/sandbox
#
# Afterwards point the sandbox-service at the artifacts:
#   BOSS_SANDBOX_BACKEND=firecracker
#   BOSS_SANDBOX_FC_KERNEL=<artifacts>/vmlinux
#   BOSS_SANDBOX_FC_ROOTFS=<artifacts>/rootfs.ext4
#   BOSS_SANDBOX_FC_BIN=<path to firecracker>   (microvm-e2e.sh installs one)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ARTIFACTS="${1:-${BOSS_SANDBOX_ARTIFACTS:-$HOME/.boss/sandbox}}"

ALPINE_IMAGE="${BOSS_SANDBOX_ALPINE_IMAGE:-alpine:3.20}"
ROOTFS_MB="${BOSS_SANDBOX_ROOTFS_MB:-1024}"
# Firecracker CI kernel line (uncompressed vmlinux, known-good with Firecracker).
KERNEL_CI_PREFIX="firecracker-ci/v1.10/x86_64"
KERNEL_PIN="${BOSS_SANDBOX_KERNEL_KEY:-}"   # optional exact S3 key override
S3_BASE="https://s3.amazonaws.com/spec.ccfc.min"

say()  { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[setup] FAIL:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || fail "this script bakes Linux guest images — run it on a Linux x86_64 host"
[ "$(uname -m)" = "x86_64" ] || fail "only x86_64 is supported (host is $(uname -m))"
command -v docker >/dev/null || fail "docker is required to populate the rootfs"
command -v curl   >/dev/null || fail "curl is required"
MKE2FS="$(command -v mke2fs || echo /usr/sbin/mke2fs)"
[ -x "$MKE2FS" ] || fail "mke2fs not found — install e2fsprogs"

mkdir -p "$ARTIFACTS"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ---- 1. Kernel ---------------------------------------------------------------
if [ -s "$ARTIFACTS/vmlinux" ]; then
  say "kernel already present: $ARTIFACTS/vmlinux (delete it to re-fetch)"
else
  if [ -z "$KERNEL_PIN" ]; then
    say "discovering newest 5.10 CI kernel under $KERNEL_CI_PREFIX ..."
    KERNEL_PIN="$(curl -fsS "$S3_BASE/?prefix=${KERNEL_CI_PREFIX}/vmlinux-5.10&list-type=2" \
      | grep -oE "${KERNEL_CI_PREFIX}/vmlinux-5\.10\.[0-9]+" | sort -V | tail -1)"
    [ -n "$KERNEL_PIN" ] || fail "could not discover a CI kernel key; set BOSS_SANDBOX_KERNEL_KEY"
  fi
  say "fetching kernel $KERNEL_PIN"
  curl -fsSL -o "$WORK/vmlinux" "$S3_BASE/$KERNEL_PIN"
  head -c4 "$WORK/vmlinux" | grep -q $'\x7fELF' || fail "downloaded kernel is not an ELF image"
  mv "$WORK/vmlinux" "$ARTIFACTS/vmlinux"
  say "kernel ready: $ARTIFACTS/vmlinux ($(du -h "$ARTIFACTS/vmlinux" | cut -f1))"
fi

# ---- 2. Rootfs ---------------------------------------------------------------
if [ -s "$ARTIFACTS/rootfs.ext4" ]; then
  say "rootfs already present: $ARTIFACTS/rootfs.ext4 (delete it to re-bake)"
  exit 0
fi

say "populating Alpine rootfs ($ALPINE_IMAGE + python3 git nodejs) via docker export"
CTR="$(docker run -d "$ALPINE_IMAGE" sh -c \
  'apk add --no-cache python3 git nodejs npm ca-certificates && mkdir -p /opt/boss /work /root')"
docker wait "$CTR" >/dev/null
docker logs "$CTR" >"$WORK/apk.log" 2>&1 || true
grep -q "OK:" "$WORK/apk.log" || { docker rm -f "$CTR" >/dev/null; sed -n '1,20p' "$WORK/apk.log"; fail "apk add failed inside $ALPINE_IMAGE"; }

STAGE="$WORK/rootfs"
mkdir -p "$STAGE"
# --no-same-owner + excluding /dev lets this run unprivileged; boss-init mounts
# devtmpfs at boot so the static /dev contents are not needed.
docker export "$CTR" | tar -x -C "$STAGE" --no-same-owner --exclude='dev/*' --exclude='.dockerenv'
docker rm -f "$CTR" >/dev/null

say "baking the in-guest agent + boss-init"
install -m 0644 "$REPO_ROOT/sandbox-host/guest/in_guest_agent.py" "$STAGE/opt/boss/in_guest_agent.py"
cat > "$STAGE/sbin/boss-init" <<'INIT'
#!/bin/sh
# boss-init — PID 1 of a BOSS sandbox microVM. Mount the work drive, run the
# in-guest pipeline, flush, power off. The VM's whole life is one task.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sys /sys 2>/dev/null
mount -t devtmpfs dev /dev 2>/dev/null
mkdir -p /work
if ! mount /dev/vdb /work; then
  echo "boss-init: FATAL: cannot mount work drive /dev/vdb" > /dev/console
  reboot -f
fi
# Guest DNS when a tap/ip= is configured; harmless offline.
echo "nameserver 8.8.8.8" > /etc/resolv.conf
BOSS_SPEC_PATH=/work/spec.json \
BOSS_RESULT_PATH=/work/result.json \
BOSS_WORKDIR=/work/repo \
  python3 /opt/boss/in_guest_agent.py > /work/agent.log 2>&1
sync
umount /work 2>/dev/null
reboot -f
INIT
chmod 0755 "$STAGE/sbin/boss-init"

say "packing ext4 image (${ROOTFS_MB}m, no mounts, no sudo)"
"$MKE2FS" -F -q -t ext4 -m 0 -L bossroot -d "$STAGE" "$WORK/rootfs.ext4" "${ROOTFS_MB}m"
mv "$WORK/rootfs.ext4" "$ARTIFACTS/rootfs.ext4"

say "rootfs ready: $ARTIFACTS/rootfs.ext4 ($(du -h "$ARTIFACTS/rootfs.ext4" | cut -f1))"
say "done. Export for the service:"
say "  BOSS_SANDBOX_BACKEND=firecracker"
say "  BOSS_SANDBOX_FC_KERNEL=$ARTIFACTS/vmlinux"
say "  BOSS_SANDBOX_FC_ROOTFS=$ARTIFACTS/rootfs.ext4"
