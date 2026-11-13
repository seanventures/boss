#!/usr/bin/env bash
# microvm-e2e.sh — prove the Firecracker sandbox lane end to end ON A LINUX
# x86_64 HOST WITH /dev/kvm (e.g. boss-1). Prints PASS/FAIL per step:
#
#   1. host capabilities      (/dev/kvm, docker, e2fsprogs, arch)
#   2. install firecracker    (pinned release, sha256-verified, user-local)
#   3. bake kernel + rootfs   (scripts/sandbox/microvm-setup.sh)
#   4. build sandbox-service  (local go, or dockerized golang when absent)
#   5. start the service      (firecracker backend, claims unfrozen HOST-SIDE)
#   6. health is honest       (ok=true, backend=firecracker, firecracker_live=true)
#   7. one real task          (submit → poll → done → SANDBOX_OK.txt → release)
#
# Installs ONLY under the artifacts dir (default ~/.boss/sandbox) — nothing
# system-wide. It does NOT touch the BOSS control plane: the engine-side
# BOSS_MICROVM_CLAIMS_FROZEN default stays true; enabling the lane for a
# project is the separate, documented step in docs/infra/MICROVM_ENABLE.md.
#
# Usage: scripts/sandbox/microvm-e2e.sh [artifacts-dir]
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ARTIFACTS="${1:-${BOSS_SANDBOX_ARTIFACTS:-$HOME/.boss/sandbox}}"
FC_VERSION="${BOSS_SANDBOX_FC_VERSION:-v1.10.1}"
FC_TGZ="firecracker-${FC_VERSION}-x86_64.tgz"
FC_URL="https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/${FC_TGZ}"
TOKEN="${BOSS_SANDBOX_TOKEN:-e2e-$(date +%s)}"
PORT="${BOSS_SANDBOX_E2E_PORT:-8078}"
BASE="http://127.0.0.1:${PORT}"

PASSES=0; FAILS=0
SVC_PID=""
pass() { printf '\033[1;32mPASS\033[0m  %s\n' "$*"; PASSES=$((PASSES+1)); }
fail() { printf '\033[1;31mFAIL\033[0m  %s\n' "$*"; FAILS=$((FAILS+1)); summary; exit 1; }
say()  { printf '\033[1;34m[e2e]\033[0m %s\n' "$*"; }
summary() {
  echo "----------------------------------------"
  echo "microVM e2e: ${PASSES} passed, ${FAILS} failed"
  [ -n "$SVC_PID" ] && kill "$SVC_PID" 2>/dev/null
}
trap 'kill "$SVC_PID" 2>/dev/null || true' EXIT

jsonget() { # jsonget '<expr over d>' — reads JSON on stdin
  python3 -c "import json,sys; d=json.load(sys.stdin); print($1)" 2>/dev/null
}

# ---- 1. host capabilities ------------------------------------------------------
[ "$(uname -s)" = "Linux" ] && [ "$(uname -m)" = "x86_64" ] \
  || fail "host must be Linux x86_64 (this is $(uname -sm)) — run on boss-1, not the laptop"
[ -e /dev/kvm ] || fail "/dev/kvm is missing — no KVM on this host"
[ -r /dev/kvm ] && [ -w /dev/kvm ] \
  || fail "/dev/kvm exists but $(id -un) cannot open it — add the user to the kvm group (sudo usermod -aG kvm $(id -un)) and re-login"
command -v docker >/dev/null || fail "docker is required (rootfs bake + optional go build)"
command -v mke2fs >/dev/null 2>&1 || [ -x /usr/sbin/mke2fs ] || fail "mke2fs missing — install e2fsprogs"
command -v debugfs >/dev/null 2>&1 || [ -x /usr/sbin/debugfs ] || fail "debugfs missing — install e2fsprogs"
command -v curl >/dev/null || fail "curl is required"
AVAIL_GB=$(df -BG --output=avail "$HOME" | tail -1 | tr -dc 0-9)
[ "${AVAIL_GB:-0}" -ge 6 ] || fail "need >=6 GB free under \$HOME (have ${AVAIL_GB}G) — clear disk first"
pass "host capabilities (Linux x86_64, /dev/kvm rw, docker, e2fsprogs, ${AVAIL_GB}G free)"

mkdir -p "$ARTIFACTS"

# ---- 2. firecracker binary (pinned + sha256) -----------------------------------
FC_BIN="$ARTIFACTS/firecracker-${FC_VERSION}"
if [ ! -x "$FC_BIN" ]; then
  say "fetching firecracker ${FC_VERSION}"
  TMP="$(mktemp -d)"
  curl -fsSL -o "$TMP/$FC_TGZ" "$FC_URL" || fail "download $FC_URL"
  # Releases publish a sha256 companion; verify against it (pin by version).
  SHA_OK=""
  for suffix in ".sha256.txt" ".sha256"; do
    if curl -fsSL -o "$TMP/sums" "${FC_URL}${suffix}" 2>/dev/null; then
      (cd "$TMP" && grep "$FC_TGZ" sums | sha256sum -c -) && SHA_OK=1
      break
    fi
  done
  [ -n "$SHA_OK" ] || fail "sha256 verification of $FC_TGZ failed (no published checksum matched)"
  tar -xzf "$TMP/$FC_TGZ" -C "$TMP"
  FOUND="$(find "$TMP" -type f -name "firecracker-${FC_VERSION}-x86_64" | head -1)"
  [ -n "$FOUND" ] || fail "firecracker binary not found inside $FC_TGZ"
  install -m 0755 "$FOUND" "$FC_BIN"
  rm -rf "$TMP"
fi
"$FC_BIN" --version >/dev/null 2>&1 || fail "firecracker binary does not execute"
pass "firecracker ${FC_VERSION} installed at $FC_BIN (sha256 verified)"

# ---- 3. kernel + rootfs --------------------------------------------------------
bash "$SCRIPT_DIR/microvm-setup.sh" "$ARTIFACTS" || fail "microvm-setup.sh (kernel/rootfs bake)"
[ -s "$ARTIFACTS/vmlinux" ] && [ -s "$ARTIFACTS/rootfs.ext4" ] || fail "artifacts missing after setup"
pass "guest artifacts baked ($ARTIFACTS/vmlinux, $ARTIFACTS/rootfs.ext4)"

# ---- 4. build sandbox-service --------------------------------------------------
SVC_BIN="${SANDBOX_SERVICE_BIN:-$ARTIFACTS/sandbox-service}"
if [ ! -x "$SVC_BIN" ] || [ -z "${SANDBOX_SERVICE_BIN:-}" ]; then
  if command -v go >/dev/null; then
    (cd "$REPO_ROOT/services/sandbox-service" && go build -o "$SVC_BIN" ./cmd/sandbox-service) \
      || fail "go build sandbox-service"
  else
    say "no local go toolchain — building inside golang:1.22 (docker)"
    docker run --rm -v "$REPO_ROOT":/src -w /src/services/sandbox-service \
      -e GOFLAGS=-buildvcs=false -e GOCACHE=/tmp/gocache \
      golang:1.22 go build -o /src/services/sandbox-service/sandbox-service ./cmd/sandbox-service \
      || fail "dockerized go build sandbox-service"
    install -m 0755 "$REPO_ROOT/services/sandbox-service/sandbox-service" "$SVC_BIN"
    rm -f "$REPO_ROOT/services/sandbox-service/sandbox-service"
  fi
fi
pass "sandbox-service built at $SVC_BIN"

# ---- 5. start the service with the firecracker backend -------------------------
WORK_ROOT="$ARTIFACTS/work"
mkdir -p "$WORK_ROOT"
BOSS_SANDBOX_LISTEN="127.0.0.1:${PORT}" \
BOSS_SANDBOX_TOKEN="$TOKEN" \
BOSS_SANDBOX_BACKEND=firecracker \
BOSS_SANDBOX_FC_BIN="$FC_BIN" \
BOSS_SANDBOX_FC_KERNEL="$ARTIFACTS/vmlinux" \
BOSS_SANDBOX_FC_ROOTFS="$ARTIFACTS/rootfs.ext4" \
BOSS_SANDBOX_WORK_ROOT="$WORK_ROOT" \
BOSS_MICROVM_CLAIMS_FROZEN=false \
  "$SVC_BIN" >"$ARTIFACTS/service.log" 2>&1 &
SVC_PID=$!
for _ in $(seq 1 25); do
  curl -fsS "$BASE/v1/sandbox/health" >/dev/null 2>&1 && break
  kill -0 "$SVC_PID" 2>/dev/null || { sed -n '1,10p' "$ARTIFACTS/service.log"; fail "service exited at boot"; }
  sleep 0.4
done
pass "sandbox-service up on $BASE (pid $SVC_PID, log: $ARTIFACTS/service.log)"

# ---- 6. honest health ----------------------------------------------------------
HEALTH="$(curl -fsS "$BASE/v1/sandbox/health")" || fail "health endpoint unreachable"
OK="$(echo "$HEALTH" | jsonget "d['ok'] and d['firecracker_live'] and d['backend']=='firecracker'")"
[ "$OK" = "True" ] || { echo "$HEALTH"; fail "health must report ok=true backend=firecracker firecracker_live=true"; }
pass "health: ok=true, backend=firecracker, firecracker_live=true"

# ---- 7. one real task through the contract -------------------------------------
say "submitting echo task (boots a real microVM; first boot pulls nothing — all local)"
SUBMIT="$(curl -fsS -X POST "$BASE/v1/sandbox/tasks" \
  -H "X-Sandbox-Token: $TOKEN" -H 'content-type: application/json' \
  -d '{"contract_version":"1.0","task_id":"e2e_fc_echo","tenant_id":"default",
       "engine":"echo","test_mode":true,"engine_env":{},"repo":{},
       "instructions":"boss-1 firecracker e2e","timeout":240}')" \
  || fail "submit was rejected: $(curl -s -X POST "$BASE/v1/sandbox/tasks" -H "X-Sandbox-Token: $TOKEN" -H 'content-type: application/json' -d '{"contract_version":"1.0","task_id":"e2e_fc_echo","engine":"echo","test_mode":true,"engine_env":{},"repo":{}}')"
SID="$(echo "$SUBMIT" | jsonget "d['sandbox_task_id']")"
[ -n "$SID" ] || fail "submit returned no sandbox_task_id: $SUBMIT"
say "sandbox_task_id=$SID — polling"
STATUS=""
for _ in $(seq 1 90); do
  sleep 3
  BODY="$(curl -fsS "$BASE/v1/sandbox/tasks/$SID" -H "X-Sandbox-Token: $TOKEN")" || fail "poll failed"
  STATUS="$(echo "$BODY" | jsonget "d['status']")"
  case "$STATUS" in done|failed|timeout|cancelled) break ;; esac
done
if [ "$STATUS" != "done" ]; then
  echo "$BODY"
  echo "--- console.log tail ---"
  curl -s "$BASE/v1/sandbox/tasks/$SID/logs" -H "X-Sandbox-Token: $TOKEN" | jsonget "d['console'][-2000:]" || true
  fail "task finished as '$STATUS' (wanted done)"
fi
MARKER="$(echo "$BODY" | jsonget "[f['path'] for f in d['result']['diff']]")"
echo "$MARKER" | grep -q "SANDBOX_OK.txt" || fail "result diff lacks SANDBOX_OK.txt: $MARKER"
pass "microVM task done — guest pipeline produced SANDBOX_OK.txt"

curl -fsS -X DELETE "$BASE/v1/sandbox/tasks/$SID" -H "X-Sandbox-Token: $TOKEN" >/dev/null \
  || fail "release (DELETE) failed"
GONE="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/v1/sandbox/tasks/$SID" -H "X-Sandbox-Token: $TOKEN")"
[ "$GONE" = "404" ] || fail "released task should be 404, got $GONE"
pass "release freed the slot (poll now 404)"

summary
say "the host side is proven. To point a BOSS project at it, follow docs/infra/MICROVM_ENABLE.md"
