#!/bin/sh
# The api/worker entrypoint (IDENTITY.md §7.6). Starts as root ONLY to fence the sandbox uids,
# then becomes `boss` for good and execs the service.
#
#   1. Network: processes of the sandbox uid pool (61000-61999) may reach loopback, and on the
#      container's own networks ONLY the BOSS API (8000) and boss-egress (8090 git/API proxy,
#      8091 the allowlisted CONNECT proxy that is the model gateway and package mirror route).
#      Everything else — the database, Infisical, the host, the internet — is rejected.
#      BOSS_SANDBOX_NETWORK=enforce (default) refuses to start when the rules cannot be installed;
#      `off` skips them (a developer's choice, reported by posture).
#   2. The run and shim roots exist, owned by boss, 0711 (a sandbox uid can traverse to its own
#      run directory, never list or enter another).
#   3. The identity key volume and the artifacts are boss-only.
#   4. exec as boss, keeping in the bounding set only what boss-sandbox-exec's file capabilities
#      need; nothing else can ever be regained.
set -eu

UID_RANGE="61000-61999"
RUN_ROOT="${BOSS_SANDBOX_RUN_ROOT:-/var/lib/boss/sandboxes}"
SHIM_ROOT="/run/boss-shim"
PORTS="${BOSS_SANDBOX_ALLOWED_PORTS:-8000,8090,8091}"
MODE="${BOSS_SANDBOX_NETWORK:-enforce}"

if [ "$(id -u)" != "0" ]; then
  exec "$@"
fi

fence_network() {
  command -v iptables >/dev/null 2>&1 || return 1
  iptables -N BOSS_SANDBOX 2>/dev/null || iptables -F BOSS_SANDBOX || return 1
  iptables -D OUTPUT -m owner --uid-owner "$UID_RANGE" -j BOSS_SANDBOX 2>/dev/null || true
  iptables -A BOSS_SANDBOX -o lo -j ACCEPT || return 1
  iptables -A BOSS_SANDBOX -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT || return 1
  # The container's own (non-default-route) networks: the compose network, by subnet.
  for net in $(python3 - <<'PY'
import socket, struct
with open("/proc/net/route") as fh:
    next(fh)
    for line in fh:
        f = line.split()
        dest, mask = int(f[1], 16), int(f[7], 16)
        if dest == 0 or mask == 0:
            continue
        print(f"{socket.inet_ntoa(struct.pack('<I', dest))}/{bin(mask).count('1')}")
PY
  ); do
    iptables -A BOSS_SANDBOX -d "$net" -p tcp -m multiport --dports "$PORTS" -j ACCEPT || return 1
  done
  iptables -A BOSS_SANDBOX -j REJECT || return 1
  iptables -A OUTPUT -m owner --uid-owner "$UID_RANGE" -j BOSS_SANDBOX || return 1
  if command -v ip6tables >/dev/null 2>&1 && ip6tables -L >/dev/null 2>&1; then
    ip6tables -N BOSS_SANDBOX 2>/dev/null || ip6tables -F BOSS_SANDBOX
    ip6tables -A BOSS_SANDBOX -o lo -j ACCEPT
    ip6tables -A BOSS_SANDBOX -j REJECT
    ip6tables -A OUTPUT -m owner --uid-owner "$UID_RANGE" -j BOSS_SANDBOX
  fi
  return 0
}

mkdir -p "$SHIM_ROOT"
rm -f "$SHIM_ROOT/.network-fenced"
case "$MODE" in
  off) echo "boss: BOSS_SANDBOX_NETWORK=off — sandboxed builds can reach this container's networks" >&2 ;;
  *)
    if ! fence_network; then
      echo "boss: could not install the sandbox network rules (needs iptables and cap_add NET_ADMIN)." >&2
      echo "boss: refusing to start. Set BOSS_SANDBOX_NETWORK=off only on a machine you accept that on." >&2
      exit 1
    fi
    # The API refuses to start an isolated build without this marker (sandbox_identity.available).
    : > "$SHIM_ROOT/.network-fenced"
    chown root:root "$SHIM_ROOT/.network-fenced"
    chmod 0444 "$SHIM_ROOT/.network-fenced" ;;
esac

mkdir -p "$RUN_ROOT" "$SHIM_ROOT"
chown boss:boss "$RUN_ROOT" "$SHIM_ROOT"
chmod 0711 "$RUN_ROOT" "$SHIM_ROOT"
for d in /var/lib/boss/identity /var/lib/boss/artifacts; do
  if [ -d "$d" ]; then chown boss:boss "$d"; chmod 0750 "$d"; fi
done
chmod 0700 /var/lib/boss/identity 2>/dev/null || true

exec setpriv --reuid=boss --regid=boss --init-groups --inh-caps=-all \
  --bounding-set=-all,+setuid,+setgid,+chown,+fowner -- "$@"
