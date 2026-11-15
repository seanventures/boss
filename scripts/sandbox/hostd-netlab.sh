#!/usr/bin/env bash
# hostd-netlab.sh — prove the replica tap policy end to end in network
# namespaces (no VM, nothing touched outside the namespaces). Needs root,
# nft >= 1.0.7 and a kernel with nft GRE inner matching (>= 6.2).
#
#   sudo scripts/sandbox/hostd-netlab.sh <boss-hostd binary>
#
#   p8c  "director + caller": outer 192.0.2.10 (a director), inner caller 198.51.100.5;
#        sends to the VIP through GRE (as IPVS --tun-type gre would)
#   p8h  "host": routes 10.200.4.11/32 to the tap, the EXACT nft rules boss-hostd
#        applies (boss-hostd netpol-render), proxy-ARP, ip_forward
#   p8v  "replica VM": eth0 10.200.4.11/32 via 169.254.1.1, VIP on lo, GRE decap,
#        an HTTP server on VIP:443; replies go straight back (DSR), not via GRE
#
# Checks: (1) a caller's request gets its reply; (2) the reply was accepted on
# the learned flow and FIN shortened the flow's timeout; (3) a forged non-SYN
# packet from VIP:443 to a never-seen peer is dropped and counted; (4) a VM
# packet to an internal net is dropped; (5) GRE from a non-director is dropped.
set -euo pipefail
BIN=${1:?boss-hostd binary}
TAP=bt0000000000aa
pass=0; fail=0
ok()  { echo "PASS: $*"; pass=$((pass+1)); }
bad() { echo "FAIL: $*"; fail=$((fail+1)); }
cleanup() {
  for ns in p8c p8h p8v; do ip netns pids $ns 2>/dev/null | xargs -r kill 2>/dev/null || true; ip netns del $ns 2>/dev/null || true; done
}
[ -n "${KEEP:-}" ] || trap cleanup EXIT
cleanup
for ns in p8c p8h p8v; do ip netns add $ns; ip -n $ns link set lo up; done
H="ip netns exec p8h"; C="ip netns exec p8c"; V="ip netns exec p8v"

ip link add c0 netns p8c type veth peer name h0 netns p8h
ip link add $TAP netns p8h type veth peer name eth0 netns p8v
$H sysctl -qw net.ipv4.ip_forward=1 net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0
$H ip addr add 192.0.2.1/24 dev h0; $H ip link set h0 up
$H ip link set $TAP up; $H sysctl -qw net.ipv4.conf.$TAP.proxy_arp=1 net.ipv4.conf.$TAP.rp_filter=0
$H ip route add 10.200.4.11/32 dev $TAP
$H ip route add 198.51.100.0/24 via 192.0.2.10
$H ip route add default via 192.0.2.10   # a real host has one; proxy-ARP needs 169.254.1.1 routable

$C sysctl -qw net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0
$C ip addr add 192.0.2.10/24 dev c0; $C ip addr add 192.0.2.99/24 dev c0; $C ip link set c0 up
$C ip addr add 198.51.100.5/32 dev lo
$C ip route add 10.200.4.0/23 via 192.0.2.1
$C ip tunnel add gre1 mode gre local 192.0.2.10 remote 10.200.4.11 ttl 64
$C ip link set gre1 up
$C ip route add 203.0.113.17/32 dev gre1 src 198.51.100.5
$C sysctl -qw net.ipv4.conf.gre1.rp_filter=0 net.ipv4.conf.c0.rp_filter=0

$V sysctl -qw net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0 \
  net.ipv4.conf.all.arp_announce=2 net.ipv4.conf.all.arp_ignore=1 net.ipv6.conf.all.disable_ipv6=1
$V ip addr add 10.200.4.11/32 dev eth0; $V ip link set eth0 up
$V ip route add 169.254.1.1/32 dev eth0 scope link
$V ip route add default via 169.254.1.1 dev eth0 onlink
$V ip addr add 203.0.113.17/32 dev lo
$V ip tunnel add gre1 mode gre local 10.200.4.11 remote 192.0.2.10 ttl 64
$V ip link set gre1 up; $V sysctl -qw net.ipv4.conf.gre1.rp_filter=0

"$BIN" netpol-render --tap $TAP --vm-ip 10.200.4.11 --vip 203.0.113.17 --vip-ports 443 \
  --vm-prefix 10.200.4.0/23 --directors 192.0.2.10 --internal-nets 192.0.2.0/24 > /tmp/p8-netlab.nft
$H nft -f /tmp/p8-netlab.nft
cnt() { $H nft -j list counter inet boss_hostd "$1_$TAP" | python3 -c 'import json,sys; d=json.load(sys.stdin); print([x["counter"]["packets"] for x in d["nftables"] if "counter" in x][0])'; }

$V python3 -m http.server 443 --bind 203.0.113.17 >/dev/null 2>&1 &
sleep 1

# (1) a caller's request through GRE gets its direct reply
if out=$($C curl -s -m 5 -o /dev/null -w '%{http_code}' http://203.0.113.17:443/); [ "$out" = 200 ]; then
  ok "caller 198.51.100.5 → VIP via GRE: HTTP 200, reply returned direct from the VIP"; else bad "request failed ($out)"; fi
sleep 0.3
r=$(cnt reply); o=$(cnt orig); i=$(cnt in)
[ "$r" -gt 0 ] && [ "$o" -eq 0 ] && [ "$i" -gt 0 ] && ok "counters after the request: in=$i reply=$r orig=0" || bad "counters in=$i reply=$r orig=$o"
# (2) the flow was learned from the SYN, and the server's FIN moved it to closing_ (30 s)
flows=$($H nft list set inet boss_hostd flows_$TAP | grep elements || true)
closing=$($H nft list set inet boss_hostd closing_$TAP | grep elements || true)
if [ -z "$flows" ] && echo "$closing" | grep -q "203.0.113.17 . 198.51.100.5 . 443 . "; then
  ok "flow (VIP, caller, 443, caller port) learned on SYN; FIN moved it to closing_ (30 s): $closing"; else bad "flows=[$flows] closing=[$closing]"; fi
# (3) a forged non-SYN from VIP:443 to a never-seen peer port is dropped and counted
o0=$(cnt orig)
$V python3 - <<'PY'
import socket, struct
def csum(b):
    if len(b) % 2: b += b"\0"
    s = sum(struct.unpack("!%dH" % (len(b)//2), b)); s = (s >> 16) + (s & 0xffff); s += s >> 16
    return ~s & 0xffff
src, dst = socket.inet_aton("203.0.113.17"), socket.inet_aton("198.51.100.5")
tcp = struct.pack("!HHIIBBHHH", 443, 55555, 1, 1, 5 << 4, 0x10, 1024, 0, 0)  # ACK, no SYN
pseudo = src + dst + struct.pack("!BBH", 0, 6, len(tcp))
tcp = tcp[:16] + struct.pack("!H", csum(pseudo + tcp)) + tcp[18:]
s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_TCP)
s.bind(("203.0.113.17", 0))
for _ in range(3): s.sendto(tcp + b"exfil", ("198.51.100.5", 0))
PY
sleep 0.3; o1=$(cnt orig)
[ "$o1" -ge $((o0+3)) ] && ok "3 forged ACKs VIP:443 → unseen peer:55555 dropped (orig $o0 → $o1)" || bad "forged packets not counted ($o0 → $o1)"
# (4) a VM packet to an internal net (the infra range 192.0.2.0/24) is dropped
o0=$(cnt orig)
$V curl -s -m 2 -o /dev/null http://192.0.2.1:22/ || true
o1=$(cnt orig)
[ "$o1" -gt "$o0" ] && ok "VM → infra 192.0.2.1 dropped (orig $o0 → $o1)" || bad "internal net not dropped"
# (5) GRE from a non-director source is dropped
d0=$(cnt indrop)
$C ip tunnel add gre2 mode gre local 192.0.2.99 remote 10.200.4.11 ttl 64; $C ip link set gre2 up
$C ip route add 203.0.113.18/32 dev gre2 src 198.51.100.5
$C curl -s -m 2 -o /dev/null http://203.0.113.18:443/ || true
d1=$(cnt indrop)
[ "$d1" -gt "$d0" ] && ok "GRE from non-director 192.0.2.99 dropped (indrop $d0 → $d1)" || bad "non-director GRE not dropped"
rm -f /tmp/p8-netlab.nft
echo "netlab: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
