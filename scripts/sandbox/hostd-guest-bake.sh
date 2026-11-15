#!/usr/bin/env bash
# hostd-guest-bake.sh — make a boss-hostd guest rootfs from a base rootfs by
# adding boss-vsockd as /sbin/boss-vsockd (the guest init for P8a VMs).
#
#   scripts/sandbox/hostd-guest-bake.sh <base rootfs.ext4> <boss-vsockd> <out.ext4>
#
# No root needed: the copy is sparse and the binary is written with debugfs.
# The result is mounted read-only by every VM on the host (LLD §3.4). P8b
# replaces this with the signed image pipeline (agent/coder flavours).
set -euo pipefail
base=${1:?base rootfs}; bin=${2:?boss-vsockd binary}; out=${3:?output image}
debugfs=$(command -v debugfs || echo /usr/sbin/debugfs)
[ -x "$debugfs" ] || { echo "debugfs not found (install e2fsprogs)" >&2; exit 1; }
file "$bin" 2>/dev/null | grep -q "statically linked" || echo "warning: $bin may not be static" >&2
cp --sparse=always "$base" "$out"
"$debugfs" -w -R "rm /sbin/boss-vsockd" "$out" >/dev/null 2>&1 || true
"$debugfs" -w -R "write $bin /sbin/boss-vsockd" "$out" >/dev/null
"$debugfs" -w -R "sif /sbin/boss-vsockd mode 0100755" "$out" >/dev/null
"$debugfs" -w -R "sif /sbin/boss-vsockd uid 0" "$out" >/dev/null
"$debugfs" -w -R "sif /sbin/boss-vsockd gid 0" "$out" >/dev/null
"$debugfs" -R "stat /sbin/boss-vsockd" "$out" 2>/dev/null | head -3
chmod 0644 "$out"
echo "baked $out"
