#!/bin/sh
# The api/worker entrypoint (IDENTITY.md §7.6). Starts as root ONLY to fence the sandbox uids,
# then becomes `boss` for good and execs the service.
#
#   1. Network: processes of the sandbox uid pool (61000-61999) may reach loopback, and on the
#      container's own networks ONLY the BOSS API (8000) and boss-egress (8090 git/API proxy,
#      8091 the allowlisted CONNECT proxy that is the model gateway and package mirror route,
#      and the link gateway's ports BOSS_LINK_PORTS, 18100-18199: one per cross-app link, F5).
#      App previews listen in BOSS_PREVIEW_PORTS (19000-19999), refused to the pool even on
#      loopback: a build reaches another app's preview only through the link gateway, which
#      checks the build's link pass. Previews themselves run as a pool uid too (preview_service),
#      so the same rules fence them; the one exception is a preview's OWN port: pool uid
#      61000+i may reach port <first preview port>+i on loopback, which is the port
#      preview_service gives the preview holding that uid (an app calling itself, e.g. SSR).
#      Everything else — the database, Infisical, the host, the internet — is rejected.
#      BOSS_SANDBOX_NETWORK=enforce (default): when the rules cannot be installed (rootless
#      Docker, gVisor, some WSL2 and VPS kernels without iptables owner match), BOSS STILL STARTS,
#      says so loudly, and holds every build ("Sandbox unavailable") until the host is fixed: the
#      console, people and access keep working, and nothing ever runs unfenced.
#      `required` refuses to start instead. `off` skips the rules (builds then reach this
#      container's networks: your choice, reported by posture and boss doctor).
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
LINK_PORTS="$(echo "${BOSS_LINK_PORTS:-18100-18199}" | tr '-' ':')"
PREVIEW_PORTS="$(echo "${BOSS_PREVIEW_PORTS:-19000-19999}" | tr '-' ':')"
MODE="${BOSS_SANDBOX_NETWORK:-enforce}"

if [ "$(id -u)" != "0" ]; then
  exec "$@"
fi

fence_network() {
  command -v iptables >/dev/null 2>&1 || return 1
  iptables -N BOSS_SANDBOX 2>/dev/null || iptables -F BOSS_SANDBOX || return 1
  iptables -D OUTPUT -m owner --uid-owner "$UID_RANGE" -j BOSS_SANDBOX 2>/dev/null || true
  # Loopback ADDRESSES only (127/8: the API on this container, Docker's DNS, the build's own dev
  # servers). Traffic to this container's own network address also rides `lo`, and is held to the
  # same port rules as any other address below.
  # Previews first: no build or preview reaches an app's preview directly, on any address
  # (F5 §5.4), except a preview its own port on loopback (BOSS_PREVIEW_SELF, one rule per uid).
  preview_self_rules() {
    uid_low=${UID_RANGE%%-*}; uid_high=${UID_RANGE##*-}
    port_low=${PREVIEW_PORTS%%:*}; port_high=${PREVIEW_PORTS##*:}
    echo "*filter"
    echo ":BOSS_PREVIEW_SELF - [0:0]"
    i=0
    while [ $((uid_low + i)) -le "$uid_high" ] && [ $((port_low + i)) -le "$port_high" ]; do
      echo "-A BOSS_PREVIEW_SELF -o lo -p tcp -m owner --uid-owner $((uid_low + i)) --dport $((port_low + i)) -j ACCEPT"
      i=$((i + 1))
    done
    echo "COMMIT"
  }
  preview_self_rules | iptables-restore --noflush || return 1
  iptables -A BOSS_SANDBOX -p tcp --dport "$PREVIEW_PORTS" -j BOSS_PREVIEW_SELF || return 1
  iptables -A BOSS_SANDBOX -p tcp --dport "$PREVIEW_PORTS" -j REJECT || return 1
  iptables -A BOSS_SANDBOX -o lo -d 127.0.0.0/8 -j ACCEPT || return 1
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
    iptables -A BOSS_SANDBOX -d "$net" -p tcp -m multiport --dports "$PORTS,$LINK_PORTS" -j ACCEPT || return 1
  done
  iptables -A BOSS_SANDBOX -j REJECT || return 1
  iptables -A OUTPUT -m owner --uid-owner "$UID_RANGE" -j BOSS_SANDBOX || return 1
  if command -v ip6tables >/dev/null 2>&1 && ip6tables -L >/dev/null 2>&1; then
    ip6tables -N BOSS_SANDBOX 2>/dev/null || ip6tables -F BOSS_SANDBOX
    # The same preview rules on ::1 (a Node app listening on `::` is reachable there too).
    if preview_self_rules | ip6tables-restore --noflush; then
      ip6tables -A BOSS_SANDBOX -p tcp --dport "$PREVIEW_PORTS" -j BOSS_PREVIEW_SELF
    fi
    ip6tables -A BOSS_SANDBOX -p tcp --dport "$PREVIEW_PORTS" -j REJECT
    ip6tables -A BOSS_SANDBOX -o lo -d ::1/128 -j ACCEPT
    ip6tables -A BOSS_SANDBOX -j REJECT
    ip6tables -A OUTPUT -m owner --uid-owner "$UID_RANGE" -j BOSS_SANDBOX
  fi
  return 0
}

mkdir -p "$SHIM_ROOT"
rm -f "$SHIM_ROOT/.network-fenced" "$SHIM_ROOT/.network-unfenced"
case "$MODE" in
  off) echo "boss: BOSS_SANDBOX_NETWORK=off — sandboxed builds can reach this container's networks" >&2 ;;
  *)
    if ! fence_network; then
      echo "boss: ************************************************************************" >&2
      echo "boss: could not install the sandbox network rules (needs iptables with the owner" >&2
      echo "boss: match, and cap_add NET_ADMIN). Rootless Docker, gVisor and some WSL2 and VPS" >&2
      echo "boss: kernels cannot do this." >&2
      if [ "$MODE" = "required" ]; then
        echo "boss: BOSS_SANDBOX_NETWORK=required: refusing to start." >&2
        exit 1
      fi
      echo "boss: BOSS IS STARTING WITHOUT IT: builds are held until this is fixed. Everything" >&2
      echo "boss: else works. Fix the host, or set BOSS_SANDBOX_NETWORK=off in .env if you accept" >&2
      echo "boss: builds reaching this container's networks. See docs/community/SANDBOX.md." >&2
      echo "boss: ************************************************************************" >&2
      # Read by the API (sandbox_identity.network_problem) and boss doctor: why builds are held.
      echo "iptables owner-match rules could not be installed" > "$SHIM_ROOT/.network-unfenced"
      chmod 0444 "$SHIM_ROOT/.network-unfenced"
    else
    # The API refuses to start an isolated build without this marker (sandbox_identity.available).
    : > "$SHIM_ROOT/.network-fenced"
    chown root:root "$SHIM_ROOT/.network-fenced"
    chmod 0444 "$SHIM_ROOT/.network-fenced"
    fi ;;
esac

# --- heal_ownership (begin) ---------------------------------------------------------------------
# Heal what a root `docker compose exec api …` wrote before BOSS code dropped root on import
# (boss_engine/_runtime_user.py): an install seeded as root failed its first build on a root-owned
# artifact directory. This runs AS ROOT over volumes other containers write, so it NEVER follows a
# symlink (D2): find -P never descends one and -xdev never leaves the volume; only real
# directories and regular files are chowned (a planted link is skipped, never its target); and
# chown -h, run by -execdir from inside each directory, never dereferences the name it is given.
heal_ownership() {
  find -P "$1" -xdev -mindepth 1 \( -type d -o -type f \) ! -user "$2" \
    -execdir chown -h "$2:$2" {} + 2>/dev/null || true
}
# --- heal_ownership (end) -----------------------------------------------------------------------

mkdir -p "$RUN_ROOT" "$SHIM_ROOT"
chown boss:boss "$RUN_ROOT" "$SHIM_ROOT"
chmod 0711 "$RUN_ROOT" "$SHIM_ROOT"
for d in /var/lib/boss/identity /var/lib/boss/artifacts; do
  if [ -d "$d" ] && [ ! -L "$d" ]; then
    chown -h boss:boss "$d"; chmod 0750 "$d"
    heal_ownership "$d" boss
  fi
done
chmod 0700 /var/lib/boss/identity 2>/dev/null || true

exec setpriv --reuid=boss --regid=boss --init-groups --inh-caps=-all \
  --bounding-set=-all,+setuid,+setgid,+chown,+fowner -- "$@"
