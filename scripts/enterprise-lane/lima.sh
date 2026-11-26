#!/usr/bin/env bash
# Run the enterprise lane (scripts/enterprise-lane.sh) from a Mac, in a dedicated Lima VM.
#
#   scripts/enterprise-lane/lima.sh [--vm NAME] [--adopt] [--keep-running] [-- LANE ARGS...]
#
# The VM (default `boss-ent-lane`) is the enterprise box's size: Ubuntu 24.04 (AppArmor 4),
# 4 vCPU, 8 GiB, 30 GiB, in Lima's plain mode (no host mounts, no port forwards), so nothing in it
# can reach this Mac's files or another install. It is created on the first run and reused after.
# This checkout's HEAD (committed work only) is copied in as a git bundle; the report comes back
# to ./enterprise-lane-report.md. The VM is stopped at the end unless --keep-running.
#
# Needs Lima (`brew install lima`) and 40 GB free. Never touches another Lima/Colima profile,
# Docker Desktop or an install on this Mac.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VM=boss-ent-lane
KEEP=0
ADOPT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --vm) VM="${2:?--vm needs a name}"; shift 2 ;;
    --keep-running) KEEP=1; shift ;;
    --adopt) ADOPT=1; shift ;;
    --) shift; break ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
command -v limactl >/dev/null || { echo "Lima is not installed: brew install lima" >&2; exit 2; }
free_gb=$(df -Pk "$HOME" | awk 'NR==2 {print int($4/1048576)}')
[ "$free_gb" -ge 40 ] || { echo "only ${free_gb} GB free on this Mac; the lane VM needs 40 GB" >&2; exit 2; }

if ! limactl list -q 2>/dev/null | grep -qx "$VM"; then
  limactl create --name="$VM" --tty=false --cpus=4 --memory=8 --disk=30 --vm-type=vz --plain \
    template:ubuntu-24.04
  ADOPT=1   # made here, so it is the lane's own
fi
[ "$(limactl list --format '{{.Status}}' "$VM")" = Running ] || limactl start --tty=false "$VM"
# The lane removes every container and the checkout in its VM, so it runs only in a VM it made:
# marked when created here, or adopted once with --adopt. Any other VM is refused untouched.
MARK=.boss-ent-lane-vm
if [ "$ADOPT" = 1 ]; then
  limactl shell "$VM" -- bash -lc "touch ~/$MARK"
elif ! limactl shell "$VM" -- bash -lc "test -f ~/$MARK"; then
  echo "VM '$VM' was not made by this lane (no ~/$MARK): it would lose its containers and ~/boss-src." >&2
  echo "Use a lane VM (the default boss-ent-lane), or pass --adopt if this VM is a throwaway." >&2
  exit 2
fi

BUNDLE="$(mktemp -d)/boss.bundle"
git -C "$ROOT" bundle create "$BUNDLE" HEAD >/dev/null 2>&1
SHA="$(git -C "$ROOT" rev-parse HEAD)"
limactl copy "$BUNDLE" "$VM:/tmp/boss.bundle"
rm -rf "$(dirname "$BUNDLE")"
limactl shell "$VM" -- bash -lc "
  set -e
  command -v git >/dev/null || { sudo apt-get update -q && sudo DEBIAN_FRONTEND=noninteractive apt-get install -yq git; }
  # A fresh clone every run: fetching a bundle into the old one added a whole-history pack per run
  # (21 runs, 2.5 GB), disk the install then lacked. The tenth run starts like the first.
  # sudo: the previous run's install leaves root-owned files in the checkout, which a plain rm
  # cannot remove, and the clone then fails to write over them (Permission denied).
  # The previous run's containers restart when the VM boots and bind-mount paths under the
  # checkout; between the rm and the clone they recreate them as root (clone: Permission denied).
  # This VM is the lane's own throwaway box (checked above: ~/$MARK), so remove them first.
  test -f ~/$MARK
  if command -v docker >/dev/null; then sudo docker ps -aq | xargs -r sudo docker rm -f >/dev/null; fi
  sudo rm -rf ~/boss-src
  git clone -q /tmp/boss.bundle ~/boss-src
  rm -f /tmp/boss.bundle
  git -C ~/boss-src -c advice.detachedHead=false checkout -q --detach $SHA"

set +e
LANE_ARGS=""; [ $# -eq 0 ] || LANE_ARGS="$(printf '%q ' "$@")"
limactl shell "$VM" -- bash -lc "cd ~/boss-src && ENTLANE_THROWAWAY_VM=1 scripts/enterprise-lane.sh --report ~/boss-ent-lane/report.md $LANE_ARGS"
rc=$?
set -e
limactl shell "$VM" -- bash -lc "cat ~/boss-ent-lane/report.md" > "$ROOT/enterprise-lane-report.md" 2>/dev/null &&
  echo "report: $ROOT/enterprise-lane-report.md"
# Hand the space the run freed inside the VM (pruned images, build cache) back to this Mac: the
# VM's disk file only grows otherwise (a failed install grew it 8 GB, under this script's 40 GB floor).
limactl shell "$VM" -- sudo fstrim -a >/dev/null 2>&1 || true
[ "$KEEP" = 1 ] || limactl stop "$VM" >/dev/null
exit $rc
