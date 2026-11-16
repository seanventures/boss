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

# Refuse restored state before privileged initialization or any application import.
python3 /usr/local/libexec/boss/recovery_guard.py --check

UID_RANGE="61000-61999"
RUN_ROOT="${BOSS_SANDBOX_RUN_ROOT:-/var/lib/boss/sandboxes}"
SHIM_ROOT="/run/boss-shim"
PORTS="${BOSS_SANDBOX_ALLOWED_PORTS:-8000,8090,8091}"
LINK_PORTS="$(echo "${BOSS_LINK_PORTS:-18100-18199}" | tr '-' ':')"
PREVIEW_PORTS="$(echo "${BOSS_PREVIEW_PORTS:-19000-19999}" | tr '-' ':')"
# Build services (build_services.py): the per-run Postgres server's port, never the BOSS
# database's 5432. A run reaches only its own database there (its own role; CONNECT revoked).
BSVC_PORT="${BOSS_BUILD_SERVICES_SANDBOX_PORT:-}"
case "$BSVC_PORT" in
  ''|5432) ;;
  *[!0-9]*) echo "boss: BOSS_BUILD_SERVICES_SANDBOX_PORT must be a port number; ignored" >&2 ;;
  *) PORTS="$PORTS,$BSVC_PORT" ;;
esac
MODE="${BOSS_SANDBOX_NETWORK:-enforce}"

if [ "$(id -u)" != "0" ]; then
  exec "$@"
fi

ipv6_unavailable() {
  # Missing rules are safe only when the kernel cannot create an IPv6 socket.
  # An empty address list or disabled interface is not proof: another interface may appear.
  python3 - <<'PY'
import errno
import socket
import sys
try:
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
except OSError as exc:
    sys.exit(0 if exc.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT) else 1)
else:
    sock.close()
    sys.exit(1)
PY
}

fence_ipv6() {
  if ipv6_unavailable; then
    return 0
  fi
  command -v ip6tables >/dev/null 2>&1 || return 1
  ip6tables -L >/dev/null 2>&1 || return 1
  ip6tables -N BOSS_SANDBOX 2>/dev/null || ip6tables -F BOSS_SANDBOX || return 1
  ip6tables -D OUTPUT -m owner --uid-owner "$UID_RANGE" -j BOSS_SANDBOX 2>/dev/null || true
  # The same preview rules on ::1 (a Node app listening on `::` is reachable there too).
  # preview_self_rules is defined by fence_network, which runs first.
  # A missing or failed ip6tables-restore must not be skipped: IPv4 success is not a fence.
  preview_self_rules | ip6tables-restore --noflush || return 1
  ip6tables -A BOSS_SANDBOX -p tcp --dport "$PREVIEW_PORTS" -j BOSS_PREVIEW_SELF || return 1
  ip6tables -A BOSS_SANDBOX -p tcp --dport "$PREVIEW_PORTS" -j REJECT || return 1
  ip6tables -A BOSS_SANDBOX -o lo -d ::1/128 -j ACCEPT || return 1
  ip6tables -A BOSS_SANDBOX -j REJECT || return 1
  ip6tables -A OUTPUT -m owner --uid-owner "$UID_RANGE" -j BOSS_SANDBOX || return 1
  # Do not issue the fenced marker unless the required rules can also be read back.
  ip6tables -C BOSS_SANDBOX -o lo -d ::1/128 -j ACCEPT || return 1
  ip6tables -C BOSS_SANDBOX -j REJECT || return 1
  ip6tables -C OUTPUT -m owner --uid-owner "$UID_RANGE" -j BOSS_SANDBOX || return 1
}

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
  fence_ipv6 || return 1
}

# THE SEALED SUB-POOL (the Acceptance Agent's sandbox, sandbox_identity.SEALED_BASE..): the top
# 100 pool uids get their own chain AHEAD of the pool's, LOOPBACK ONLY — not the BOSS API on the
# compose network, not boss-egress or its CONNECT proxy, not Docker's DNS (127.0.0.11, a way out
# by name), not any app preview. The API refuses a sealed sandbox without .network-sealed.
SEALED_RANGE="$(( ${UID_RANGE%%-*} + 900 ))-${UID_RANGE##*-}"
# LOOPBACK ONLY TO ITS OWN LISTENERS (BOSSAPP-1318). A per-session network namespace would be
# cleanest, but Docker's default seccomp refuses unshare(CLONE_NEWNET) without CAP_SYS_ADMIN. So
# every loopback connection a sealed uid OPENS is marked with that uid (BOSS_SEALED_OWN,
# CONNMARK), and any packet sent on a marked connection by ANOTHER socket owner — another run's
# dev server, a runtime service, BOSS itself answering — is rejected (BOSS_SEALED_REPLY). The
# session's own server (same uid) still answers; a listener of any other uid never completes.
sealed_own_rules() {
  low=${SEALED_RANGE%%-*}; high=${SEALED_RANGE##*-}
  echo "*filter"
  echo ":BOSS_SEALED_OWN - [0:0]"
  echo ":BOSS_SEALED_REPLY - [0:0]"
  i=0
  while [ $((low + i)) -le "$high" ]; do
    mark=$((0x5EA10000 + i))
    echo "-A BOSS_SEALED_OWN -m owner --uid-owner $((low + i)) -m conntrack --ctstate NEW -j CONNMARK --set-xmark $mark/0xffffffff"
    # Inbound (N3): a sealed uid never answers a connection it did not open itself — another
    # uid connecting to the session's listener never completes the handshake.
    echo "-A BOSS_SEALED_OWN -m owner --uid-owner $((low + i)) -m connmark ! --mark $mark/0xffffffff -j REJECT"
    echo "-A BOSS_SEALED_REPLY -m connmark --mark $mark/0xffffffff -m owner ! --uid-owner $((low + i)) -j REJECT"
    i=$((i + 1))
  done
  echo "COMMIT"
}

seal_network() {
  sealed_own_rules | iptables-restore --noflush || return 1
  iptables -D OUTPUT -o lo -j BOSS_SEALED_REPLY 2>/dev/null || true
  iptables -N BOSS_SEALED 2>/dev/null || iptables -F BOSS_SEALED || return 1
  iptables -D OUTPUT -m owner --uid-owner "$SEALED_RANGE" -j BOSS_SEALED 2>/dev/null || true
  iptables -A BOSS_SEALED -d 127.0.0.11/32 -j REJECT || return 1
  iptables -A BOSS_SEALED -p tcp --dport "$PREVIEW_PORTS" -j REJECT || return 1
  # BOSS's own service ports (the API, egress, link ports) even on loopback.
  iptables -A BOSS_SEALED -p tcp -m multiport --dports "$PORTS" -j REJECT || return 1
  # The link gateway's ports (one per cross-app link): never from the acceptance sandbox.
  iptables -A BOSS_SEALED -p tcp --dport "$LINK_PORTS" -j REJECT || return 1
  iptables -A BOSS_SEALED -o lo -j BOSS_SEALED_OWN || return 1
  # TCP only (N2): a one-way UDP datagram needs no reply, so the reply rule cannot stop it.
  iptables -A BOSS_SEALED -o lo -d 127.0.0.0/8 -p tcp -j ACCEPT || return 1
  iptables -A BOSS_SEALED -j REJECT || return 1
  iptables -I OUTPUT 1 -m owner --uid-owner "$SEALED_RANGE" -j BOSS_SEALED || return 1
  iptables -I OUTPUT 1 -o lo -j BOSS_SEALED_REPLY || return 1
  iptables -C OUTPUT -o lo -j BOSS_SEALED_REPLY || return 1
  iptables -C BOSS_SEALED -o lo -j BOSS_SEALED_OWN || return 1
  iptables -C BOSS_SEALED -p tcp --dport "$LINK_PORTS" -j REJECT || return 1
  iptables -C BOSS_SEALED -j REJECT || return 1
  iptables -C OUTPUT -m owner --uid-owner "$SEALED_RANGE" -j BOSS_SEALED || return 1
  if ! ipv6_unavailable; then
    sealed_own_rules | ip6tables-restore --noflush || return 1
    ip6tables -D OUTPUT -o lo -j BOSS_SEALED_REPLY 2>/dev/null || true
    ip6tables -N BOSS_SEALED 2>/dev/null || ip6tables -F BOSS_SEALED || return 1
    ip6tables -D OUTPUT -m owner --uid-owner "$SEALED_RANGE" -j BOSS_SEALED 2>/dev/null || true
    ip6tables -A BOSS_SEALED -p tcp --dport "$PREVIEW_PORTS" -j REJECT || return 1
    ip6tables -A BOSS_SEALED -p tcp -m multiport --dports "$PORTS" -j REJECT || return 1
    ip6tables -A BOSS_SEALED -p tcp --dport "$LINK_PORTS" -j REJECT || return 1
    ip6tables -A BOSS_SEALED -o lo -j BOSS_SEALED_OWN || return 1
    ip6tables -A BOSS_SEALED -o lo -d ::1/128 -p tcp -j ACCEPT || return 1
    ip6tables -A BOSS_SEALED -j REJECT || return 1
    ip6tables -I OUTPUT 1 -m owner --uid-owner "$SEALED_RANGE" -j BOSS_SEALED || return 1
    ip6tables -I OUTPUT 1 -o lo -j BOSS_SEALED_REPLY || return 1
    ip6tables -C OUTPUT -o lo -j BOSS_SEALED_REPLY || return 1
    ip6tables -C OUTPUT -m owner --uid-owner "$SEALED_RANGE" -j BOSS_SEALED || return 1
  fi
}

mkdir -p "$SHIM_ROOT"
rm -f "$SHIM_ROOT/.network-fenced" "$SHIM_ROOT/.network-unfenced" "$SHIM_ROOT/.network-sealed"
case "$MODE" in
  off) echo "boss: BOSS_SANDBOX_NETWORK=off — sandboxed builds can reach this container's networks" >&2 ;;
  *)
    if ! fence_network; then
      echo "boss: ************************************************************************" >&2
      echo "boss: could not install the sandbox network rules (needs IPv4 and usable IPv6" >&2
      echo "boss: iptables with the owner" >&2
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
      echo "IPv4/IPv6 owner-match rules could not be installed" > "$SHIM_ROOT/.network-unfenced"
      chmod 0444 "$SHIM_ROOT/.network-unfenced"
    else
    # The API refuses to start an isolated build without this marker (sandbox_identity.available).
    : > "$SHIM_ROOT/.network-fenced"
    chown root:root "$SHIM_ROOT/.network-fenced"
    chmod 0444 "$SHIM_ROOT/.network-fenced"
    # The Acceptance Agent's loopback-only sandbox: refused (never unsealed) without this marker.
    if seal_network; then
      : > "$SHIM_ROOT/.network-sealed"
      chown root:root "$SHIM_ROOT/.network-sealed"
      chmod 0444 "$SHIM_ROOT/.network-sealed"
    else
      echo "boss: the sealed (loopback-only) acceptance sandbox rules could not be installed;" >&2
      echo "boss: acceptance checks that run commands are refused until they are." >&2
    fi
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

# The build-services superuser's password (build_services.py; the build-services container reads
# it as root). Generated once, here, as root; readable by boss only — never by a sandbox uid.
if [ -n "${BOSS_BUILD_SERVICES_PG_PASSWORD_FILE:-}" ]; then
  bsvc_dir=$(dirname "$BOSS_BUILD_SERVICES_PG_PASSWORD_FILE")
  if [ -d "$bsvc_dir" ] && [ ! -L "$bsvc_dir" ] && [ ! -L "$BOSS_BUILD_SERVICES_PG_PASSWORD_FILE" ]; then
    if [ ! -s "$BOSS_BUILD_SERVICES_PG_PASSWORD_FILE" ]; then
      (umask 077; head -c 64 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 48 \
        > "$BOSS_BUILD_SERVICES_PG_PASSWORD_FILE")
    fi
    chown -h boss:boss "$bsvc_dir" "$BOSS_BUILD_SERVICES_PG_PASSWORD_FILE"
    chmod 0700 "$bsvc_dir"; chmod 0400 "$BOSS_BUILD_SERVICES_PG_PASSWORD_FILE"
  fi
fi

exec setpriv --reuid=boss --regid=boss --init-groups --inh-caps=-all \
  --bounding-set=-all,+setuid,+setgid,+chown,+fowner -- "$@"
