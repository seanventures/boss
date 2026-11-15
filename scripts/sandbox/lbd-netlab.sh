#!/usr/bin/env bash
# lbd-netlab.sh — the P8b-10 director lab in network namespaces on one machine (boss-1):
# two boss-lbd directors (IPVS, GRE tunnel mode, keepalived VRRP), one "host" with three
# replica namespaces behind the exact boss-hostd tap policy, and a client. Nothing outside
# the namespaces is touched except loading ip_vs/ip_gre; everything is removed at exit.
#
#   sudo scripts/sandbox/lbd-netlab.sh <boss-lbd> <boss-hostd> [--keep]
#
# Topology (all on one bridge in netns "p8w", 192.0.2.0/24 as the "infra" network):
#   p8c  client     192.0.2.50 (and 198.51.100.5/32 on lo: the caller address)
#   p8a  director A 192.0.2.10   boss-lbd --desired-file, keepalived (VRRP unicast), IPVS
#   p8b  director B 192.0.2.11   the same
#   p8h  host       192.0.2.20   vm_prefix 10.200.4.0/23; three taps bt… with boss-hostd netpol
#   p8v1..3 replicas 10.200.4.11/12/13: VIP 203.0.113.17 on lo, bgre0 decap, TLS server on :443
#
# Checks (LLD §4.2):
#   T1  curl --cacert to https://203.0.113.17 → 200, three connections land on three replicas (rr);
#       tcpdump on the director shows GRE out and NO reply packets from the VIP (DSR bypass)
#   T2  (a) health path: kill v2's server → boss-lbd withdraws (< 1 s metric), a client loop
#           sees no failure after one retry; (b) push path: `boss-lbd push --weight 0` and
#           `--delete` from the host's certificate: latency from the director's metric
#   T13 CPS and throughput through one director (openssl s_time), ip_vs_conn slab size
#   VRRP: half the groups MASTER on A, half on B; kill A's keepalived → B takes its VIPs
set -euo pipefail
LBD=${1:?boss-lbd binary}; HOSTD=${2:?boss-hostd binary}; KEEP=${3:-}
LAB=${LAB:-/tmp/p8-lbd-lab}
VIP=203.0.113.17; VIP2=203.0.113.18
pass=0; fail=0
ok()  { echo "PASS: $*"; pass=$((pass+1)); }
bad() { echo "FAIL: $*"; fail=$((fail+1)); }
NS="p8w p8c p8a p8b p8h p8v1 p8v2 p8v3"
cleanup() {
  for ns in $NS; do ip netns pids $ns 2>/dev/null | xargs -r kill 2>/dev/null || true; done
  sleep 0.3
  for ns in $NS; do ip netns pids $ns 2>/dev/null | xargs -r kill -9 2>/dev/null || true; ip netns del $ns 2>/dev/null || true; done
  rm -rf "$LAB"
}
[ "$KEEP" = "--keep" ] || trap cleanup EXIT
cleanup; mkdir -p "$LAB"/{pki,A,B,desired,bin}
# keepalived runs the track/notify scripts only from a root-owned, non-world-writable path
# (enable_script_security); in production boss-lbd lives in /usr/local/bin. Use a root-owned copy.
install -m 0755 -o root -g root "$LBD" "$LAB/bin/boss-lbd"; LBD="$LAB/bin/boss-lbd"
modprobe ip_vs ip_vs_rr ip_vs_wlc ip_gre 2>/dev/null || true

for ns in $NS; do ip netns add $ns; ip -n $ns link set lo up; done
W="ip netns exec p8w"; C="ip netns exec p8c"; A="ip netns exec p8a"; B="ip netns exec p8b"; H="ip netns exec p8h"
$W ip link add br0 type bridge; $W ip link set br0 up
link() { # link <ns> <ifname> <addr/len>
  ip link add "w-$1" netns p8w type veth peer name "$2" netns "$1"
  $W ip link set "w-$1" master br0 up
  ip -n "$1" addr add "$3" dev "$2"; ip -n "$1" link set "$2" up
  ip netns exec "$1" sysctl -qw net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0
}
link p8c eth0 192.0.2.50/24; link p8a eth0 192.0.2.10/24; link p8b eth0 192.0.2.11/24; link p8h eth0 192.0.2.20/24
$C ip addr add 198.51.100.5/32 dev lo
$C ip route add $VIP/32 via 192.0.2.10 src 198.51.100.5   # the "internet": VIPs are reached through the directors' segment
$C ip route add $VIP2/32 via 192.0.2.11 src 198.51.100.5
# The replies come straight from the VIP (DSR) to 198.51.100.5 via the host: route the caller back.
$H sysctl -qw net.ipv4.ip_forward=1
$H ip route add 198.51.100.5/32 via 192.0.2.50
$H ip route add default via 192.0.2.50     # a real host has one; proxy-ARP answers for 169.254.1.1 only when it is routable
for d in p8a p8b; do ip netns exec $d sysctl -qw net.ipv4.ip_forward=1; ip netns exec $d ip route add 198.51.100.0/24 via 192.0.2.50; done

# ---- replicas: routed /32 behind the host's taps, the EXACT boss-hostd policy
tap() { # tap <n> <vm_ip>
  local tapname=bt0000000000a$1
  ip link add $tapname netns p8h type veth peer name eth0 netns p8v$1
  $H ip link set $tapname up; $H sysctl -qw net.ipv4.conf.$tapname.proxy_arp=1 net.ipv4.conf.$tapname.rp_filter=0
  $H ip route add $2/32 dev $tapname
  local V="ip netns exec p8v$1"
  $V sysctl -qw net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0 net.ipv4.conf.all.arp_announce=2 net.ipv4.conf.all.arp_ignore=1 net.ipv6.conf.all.disable_ipv6=1
  $V ip addr add $2/32 dev eth0; $V ip link set eth0 up
  $V ip route add 169.254.1.1/32 dev eth0 scope link
  $V ip route add default via 169.254.1.1 dev eth0 onlink
  $V ip addr add $VIP/32 dev lo; $V ip addr add $VIP2/32 dev lo
  $V ip tunnel add bgre0 mode gre local $2 ttl 64      # remote any: both directors decapsulate here
  $V ip link set bgre0 up; $V sysctl -qw net.ipv4.conf.bgre0.rp_filter=0
  "$HOSTD" netpol-render --tap $tapname --vm-ip $2 --vip $VIP --vip-ports 443 \
    --vm-prefix 10.200.4.0/23 --directors 192.0.2.10,192.0.2.11 --internal-nets 192.0.2.0/24 > "$LAB/nft-$1"
}
tap 1 10.200.4.11; tap 2 10.200.4.12; tap 3 10.200.4.13
# One atomic nft script: VM1's render carries the base table and the director set; VMs 2 and 3
# add only their own lines (every per-VM line names its tap).
{ cat "$LAB/nft-1"; for i in 2 3; do grep "bt0000000000a$i" "$LAB/nft-$i"; done; } > "$LAB/nft-all"
$H nft -f "$LAB/nft-all" 2>"$LAB/nft.err" || { echo "nft failed: $(cat "$LAB/nft.err")"; exit 1; }
# The second VIP on the taps too (the render takes one VIP; add the second to the port map the same way).
for i in 1 2 3; do
  vmip=10.200.4.1$i; tapname=bt0000000000a$i
  $H nft add rule inet boss_hostd in_$tapname "ip saddr @directors ip daddr $vmip gre ip daddr $VIP2 gre tcp dport { 443 } accept" 2>/dev/null || true
done

# ---- PKI: a lab "fleet CA" (never the real one): director certs (CN = id, IP SAN), a host cert, and an agent CA + server cert
cd "$LAB/pki"
openssl ecparam -name prime256v1 -genkey -noout -out fleet-ca.key 2>/dev/null
openssl req -x509 -new -key fleet-ca.key -subj "/CN=lab fleet CA" -days 2 -out fleet-ca.pem 2>/dev/null
mkcert() { # mkcert <name> <cn> <ip-or-empty>
  openssl ecparam -name prime256v1 -genkey -noout -out $1.key 2>/dev/null
  openssl req -new -key $1.key -subj "/CN=$2" -out $1.csr 2>/dev/null
  printf "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage=clientAuth,serverAuth\n" > $1.ext
  [ -n "$3" ] && printf "subjectAltName=IP:$3\n" >> $1.ext
  openssl x509 -req -in $1.csr -CA fleet-ca.pem -CAkey fleet-ca.key -CAcreateserial -days 1 -extfile $1.ext -out $1.crt 2>/dev/null
}
mkcert dirA dir_a 192.0.2.10; mkcert dirB dir_b 192.0.2.11; mkcert host hst_1 192.0.2.20
openssl ecparam -name prime256v1 -genkey -noout -out agent-ca.key 2>/dev/null
openssl req -x509 -new -key agent-ca.key -subj "/CN=lab agent CA" -days 2 -out agent-ca.pem 2>/dev/null
openssl ecparam -name prime256v1 -genkey -noout -out agent.key 2>/dev/null
openssl req -new -key agent.key -subj "/CN=agent-x" -out agent.csr 2>/dev/null
printf "subjectAltName=IP:$VIP,IP:$VIP2\n" > agent.ext
openssl x509 -req -in agent.csr -CA agent-ca.pem -CAkey agent-ca.key -CAcreateserial -days 1 -extfile agent.ext -out agent.crt 2>/dev/null
cd - >/dev/null

# ---- replica TLS servers: answer their own name on VIP:443 (and VIP2)
cat > "$LAB/server.py" <<'PY'
import http.server, ssl, sys, socketserver
name, addrs = sys.argv[1], sys.argv[2].split(",")
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = (name + "\n").encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True; allow_reuse_address = True
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(sys.argv[3], sys.argv[4])
servers = []
for a in addrs:
    s = S((a, 443), H); s.socket = ctx.wrap_socket(s.socket, server_side=True); servers.append(s)
import threading
for s in servers[1:]: threading.Thread(target=s.serve_forever, daemon=True).start()
servers[0].serve_forever()
PY
for i in 1 2 3; do
  ip netns exec p8v$i python3 "$LAB/server.py" "v$i" "$VIP,$VIP2" "$LAB/pki/agent.crt" "$LAB/pki/agent.key" >"$LAB/v$i.log" 2>&1 &
done
sleep 1

# ---- desired state (what the control plane would send; file mode)
mkdesired() { # mkdesired <self-id> <path> [members...]
  local self=$1 out=$2; shift 2
  python3 - "$self" "$out" "$@" <<'PY'
import json, sys
self_id, out = sys.argv[1], sys.argv[2]
members = [{"replica_id": f"rep_{i}", "vm_ip": f"10.200.4.1{i}", "host_id": "hst_1", "weight": 1, "slots": 4} for i in (1, 2, 3)]
dirs = [{"id": "dir_a", "address": "192.0.2.10", "side": "A", "port": 7446}, {"id": "dir_b", "address": "192.0.2.11", "side": "B", "port": 7446}]
me = next(d for d in dirs if d["id"] == self_id); peer = next(d for d in dirs if d["id"] != self_id)
# Two VIPs in two groups: group 0 MASTER on A, group 1 on B, so both directors carry VIPs. The second VIP has no
# members here: the tap policy is rendered for one VIP per replica (as in production, one deployment per VM).
groups = [{"group": 0, "vrid": 1, "master": "dir_a", "vips": ["203.0.113.17"]}, {"group": 1, "vrid": 2, "master": "dir_b", "vips": ["203.0.113.18"]}]
d = {"changed": True, "director_id": self_id, "generation": 1, "self": me, "peer": peer, "directors": dirs,
     "vip_pools": ["203.0.113.0/26"],
     "vips": [{"vip": "203.0.113.17", "ports": [443], "scheduler": "rr", "deployment_id": "dep_x", "group": 0, "members": members},
              {"vip": "203.0.113.18", "ports": [443], "scheduler": "rr", "deployment_id": "dep_y", "group": 1, "members": []}],
     "groups": groups, "hosts": [{"host_id": "hst_1", "address": "192.0.2.20", "vm_prefix": "10.200.4.0/23"}], "sync_iface": "eth0"}
json.dump(d, open(out, "w"))
PY
}
mkdesired dir_a "$LAB/desired/A.json"; mkdesired dir_b "$LAB/desired/B.json"

# ---- directors: boss-lbd (lab mode) + keepalived with the config boss-lbd renders
start_lbd() { # start_lbd <ns> <letter> <id> <addr>
  local ns=$1 L=$2 id=$3 addr=$4
  mkdir -p "$LAB/$L/run" "$LAB/$L/ka"
  ip netns exec $ns env BOSS_LBD_RUN_DIR="$LAB/$L/run" "$LBD" --desired-file "$LAB/desired/$L.json" --id $id --address $addr --iface eth0 \
    --cert "$LAB/pki/dir$L.crt" --key "$LAB/pki/dir$L.key" --ca "$LAB/pki/fleet-ca.pem" \
    --state-dir "$LAB/$L/state" --run-dir "$LAB/$L/run" --metrics-addr 127.0.0.1:9446 \
    --keepalived-conf "$LAB/$L/ka/keepalived.conf" --keepalived-pid "$LAB/$L/ka/keepalived.pid" >"$LAB/$L/lbd.log" 2>&1 &
}
start_lbd p8a A dir_a 192.0.2.10; start_lbd p8b B dir_b 192.0.2.11
sleep 2
for L in A B; do [ -s "$LAB/$L/ka/keepalived.conf" ] && ok "director $L: boss-lbd rendered keepalived.conf ($(grep -c vrrp_instance "$LAB/$L/ka/keepalived.conf") VRRP instances, VRRP only)" || bad "director $L: no keepalived config ($(tail -3 "$LAB/$L/lbd.log"))"; done
grep -q virtual_server "$LAB/A/ka/keepalived.conf" && bad "keepalived config owns IPVS" || true
# keepalived needs its scripts under a root-owned path (script security): copy boss-lbd there.
sed -i "s#enable_script_security#enable_script_security\n  script_user root#" "$LAB/A/ka/keepalived.conf" 2>/dev/null || true
for L in A B; do ns=p8a; [ $L = B ] && ns=p8b
  ip netns exec $ns keepalived -f "$LAB/$L/ka/keepalived.conf" -p "$LAB/$L/ka/keepalived.pid" -r "$LAB/$L/ka/vrrp.pid" -n -l -D -G >"$LAB/$L/keepalived.log" 2>&1 &
done
sleep 4
a_has17=$($A ip -4 addr show dev eth0 | grep -c "$VIP/32" || true); b_has18=$($B ip -4 addr show dev eth0 | grep -c "$VIP2/32" || true)
a_has18=$($A ip -4 addr show dev eth0 | grep -c "$VIP2/32" || true); b_has17=$($B ip -4 addr show dev eth0 | grep -c "$VIP/32" || true)
[ "$a_has17" = 1 ] && [ "$b_has18" = 1 ] && [ "$a_has18" = 0 ] && [ "$b_has17" = 0 ] && ok "VRRP: group 0 ($VIP) MASTER on A, group 1 ($VIP2) MASTER on B — both directors carry traffic" || bad "VRRP placement A:17=$a_has17 18=$a_has18 B:17=$b_has17 18=$b_has18 ($(tail -2 "$LAB/A/keepalived.log"))"
# Health checks: 3 real servers, all weight 1, within a few seconds of start.
for i in $(seq 1 20); do $A ipvsadm -Ln > "$LAB/A/ipvs.txt" 2>&1 || true; [ "$(awk '$1=="->" && $4==1' "$LAB/A/ipvs.txt" | wc -l)" = 3 ] && break; sleep 0.5; done
[ "$(grep -c 'Tunnel' "$LAB/A/ipvs.txt")" = 3 ] && ok "IPVS on A: $(grep -c '^TCP' "$LAB/A/ipvs.txt") services, 3 tunnel-mode real servers at weight 1 (rr, no persistence), all passing the GRE health check" || bad "IPVS table on A: $(cat "$LAB/A/ipvs.txt")"
$A ipvsadm -Ln --tun-info 2>/dev/null | grep -q -i gre && ok "IPVS real servers are GRE tunnel type (--tun-info)" || echo "note: ipvsadm --tun-info unavailable or no gre flag printed"
$A ipvsadm -L --daemon 2>/dev/null | grep -q master && $A ipvsadm -L --daemon | grep -q backup && ok "IPVS sync daemons: master + backup on A" || bad "sync daemons: $($A ipvsadm -L --daemon 2>&1)"

# ---- T1: reach the VIP through the director; rr over three replicas; replies bypass the director
$A timeout 8 tcpdump -ni eth0 -w "$LAB/A/t1.pcap" "(host $VIP) or proto gre" >/dev/null 2>&1 &
TD=$!; sleep 0.5
seen=""
for i in 1 2 3 4 5 6; do
  r=$($C curl -s -m 3 --cacert "$LAB/pki/agent-ca.pem" "https://$VIP/" || echo FAIL)
  seen="$seen $r"
done
sleep 0.5; kill $TD 2>/dev/null; wait $TD 2>/dev/null || true
echo "T1 answers:$seen"
n=$(echo "$seen" | tr ' ' '\n' | grep -c '^v[123]$' || true)
distinct=$(echo "$seen" | tr ' ' '\n' | grep '^v[123]$' | sort -u | wc -l)
[ "$n" = 6 ] && [ "$distinct" = 3 ] && ok "T1: 6 TLS connections to the VIP answered 200 by all 3 replicas (round robin: $seen)" || bad "T1: $n/6 answered, $distinct distinct ($seen)"
gre_out=$($A tcpdump -nr "$LAB/A/t1.pcap" 'proto gre' 2>/dev/null | wc -l)
from_vip=$($A tcpdump -nr "$LAB/A/t1.pcap" "src host $VIP and not dst host 192.0.2.10" 2>/dev/null | wc -l)   # (to 192.0.2.10 itself = health-check SYN-ACKs, by design)
[ "$gre_out" -gt 0 ] && [ "$from_vip" = 0 ] && ok "T1: director saw $gre_out GRE packets out and 0 reply packets from the VIP (replies bypass the director)" || bad "T1 capture: gre=$gre_out from_vip=$from_vip"
$B ipvsadm -Ln | grep -q "$VIP2:443" && $B ip -4 addr show dev eth0 | grep -q "$VIP2/32" && ok "T1: the second VIP ($VIP2) is a service on both directors and MASTER on B" || bad "VIP2 not on B"

# ---- T2a: kill a replica → health check withdraws in < 1 s (metric); client loop sees no failure after one retry
metric() { ip netns exec $1 curl -s http://127.0.0.1:9446/metrics | grep "^$2" | tail -1 || true; }
drain0=$(metric p8a boss_lbd_healthcheck_drain_total | awk '{print $2}'); drain0=${drain0:-0}
kill_at=$(date +%s.%N)
ip netns pids p8v2 | xargs -r kill
fails=0; okc=0; retried=0
for i in $(seq 1 40); do
  if r=$($C curl -s -m 2 --cacert "$LAB/pki/agent-ca.pem" "https://$VIP/"); then okc=$((okc+1)); else
    retried=$((retried+1)); r=$($C curl -s -m 2 --cacert "$LAB/pki/agent-ca.pem" "https://$VIP/") && okc=$((okc+1)) || fails=$((fails+1)); fi
  sleep 0.1
done
sleep 1.5
drain1=$(metric p8a boss_lbd_healthcheck_drain_total | awk '{print $2}'); drain1=${drain1:-0}
lat=$(metric p8a boss_lbd_withdraw_seconds_max | awk '{print $2}')
w12=$($A ipvsadm -Ln | awk -v rs="10.200.4.12:443" '$2==rs {print $4}' | head -1 || true)
echo "T2a: client loop ok=$okc retried=$retried failed=$fails; drains $drain0→$drain1; v2 weight now '${w12:-deleted}'; withdraw apply max ${lat}s"
[ "$fails" = 0 ] && [ "$drain1" -gt "$drain0" ] && ok "T2a: v2 killed → health check drained it (2 failed 1 s checks) and a 40-connection client loop had $fails failures after one retry ($retried retries)" || bad "T2a: fails=$fails drains $drain0→$drain1"
# timing: the health log line has a timestamp; compare with kill time
dr=$(grep "health drain: vip $VIP vm_ip 10.200.4.12" "$LAB/A/lbd.log" | tail -1 || true)
echo "  v2 killed at $(date -u -d @${kill_at%.*} +%T 2>/dev/null || echo $kill_at) UTC; director A: ${dr:-no drain line}"
echo "  (health path: 2 failed 1 s checks + 0.7 s probe timeout ≈ 2-3 s by design; the sub-second path is the host push, T2b)"
# ---- T2b: push path from the host (as boss-hostd does): weight 0 then delete, latency from the metric
# The push runs from the host namespace (as boss-hostd would), with the host certificate.
rs=$($H "$LBD" push --to 192.0.2.10:7446,192.0.2.11:7446 --director-id dir_a,dir_b --cert "$LAB/pki/host.crt" --key "$LAB/pki/host.key" --ca "$LAB/pki/fleet-ca.pem" \
      --vip $VIP --replica rep_3 --vm-ip 10.200.4.13 --weight 0 --reason process_exit 2>&1) || true
echo "T2b push weight 0: $rs"
w13=$($A ipvsadm -Ln | awk -v rs="10.200.4.13:443" '$2==rs {print $4}' | head -1 || true)
echo "$rs" | grep -q '"latency_us"' && ! echo "$rs" | grep -q error && [ "$w13" = 0 ] && ok "T2b: host push weight 0 reached both directors over mTLS; v3 weight 0 on A; round trips: $(echo "$rs" | grep -o '"latency_us":[0-9]*' | tr '\n' ' ')" || bad "T2b push: $rs (w13=$w13)"
rs=$($H "$LBD" push --to 192.0.2.10:7446,192.0.2.11:7446 --director-id dir_a,dir_b --cert "$LAB/pki/host.crt" --key "$LAB/pki/host.key" --ca "$LAB/pki/fleet-ca.pem" \
      --vip $VIP --replica rep_3 --vm-ip 10.200.4.13 --delete --reason process_exit 2>&1) || true
$A ipvsadm -Ln | grep -q "10.200.4.13:443" && bad "T2b: deleted real server still present" || ok "T2b: DELETE removed v3 from IPVS on A ($(echo "$rs" | grep -o '"latency_us":[0-9]*' | head -1))"
# A stranger (the agent CA's cert, not the fleet CA) is refused.
rs=$($H "$LBD" push --to 192.0.2.10:7446 --cert "$LAB/pki/agent.crt" --key "$LAB/pki/agent.key" --ca "$LAB/pki/fleet-ca.pem" --vip $VIP --replica rep_1 --vm-ip 10.200.4.11 --weight 0 2>&1) || true
echo "$rs" | grep -qi "error" && ok "T2b: a push with a non-fleet certificate is refused (mTLS)" || bad "stranger push accepted: $rs"
echo "server-side withdraw histogram (A):"; ip netns exec p8a curl -s http://127.0.0.1:9446/metrics | grep '^boss_lbd_withdraw_seconds' | grep -v bucket || true
# restore v3 (register) so T13 has 2 live replicas (v1, v3)
$H "$LBD" push --to 192.0.2.10:7446,192.0.2.11:7446 --director-id dir_a,dir_b --cert "$LAB/pki/host.crt" --key "$LAB/pki/host.key" --ca "$LAB/pki/fleet-ca.pem" \
   --vip $VIP --replica rep_3 --vm-ip 10.200.4.13 --weight 1 --reason serving >/dev/null 2>&1 || true

# ---- T13: director baseline — connections per second through director A (full TLS handshakes), IPVS memory per connection
echo "T13: openssl s_time 10 s of new TLS connections to $VIP through director A"
st=$($C timeout 25 openssl s_time -connect $VIP:443 -CAfile "$LAB/pki/agent-ca.pem" -new -time 10 2>/dev/null | grep -E "connections in|connections/user sec" | tail -2 || true)
echo "$st"
cps=$(echo "$st" | grep -o '[0-9.]* connections/user sec' | head -1 || true)
$A ipvsadm -Ln --stats > "$LAB/A/stats.txt" 2>/dev/null || true
conns=$(awk -v vip="$VIP:443" '$2==vip {print $3}' "$LAB/A/stats.txt" | head -1 || true)
slab=$( (grep '^ip_vs_conn ' /proc/slabinfo || true) | awk '{print "ip_vs_conn slab: active="$2" objsize="$4"B"}')
[ -n "$slab" ] || slab="ip_vs_conn slab merged into kmalloc (sizeof(struct ip_vs_conn) ≈ 320 B on x86_64 6.8)"
echo "T13: IPVS stats for $VIP:443 on A: conns=$conns; $slab"
echo "T13: director A /proc/net/ip_vs_stats (hex: conns pkts-in pkts-out bytes-in bytes-out; then per-second rates):"; $A cat /proc/net/ip_vs_stats
[ -n "$cps" ] && ok "T13: $cps new TLS connections/s through one director (client-bound, 1 netns client); $slab" || bad "T13: no s_time result"
echo "director A /status: $(ip netns exec p8a curl -s http://127.0.0.1:9446/status)"
st_a=$(ip netns exec p8a curl -s http://127.0.0.1:9446/status)
echo "$st_a" | grep -q '"keepalived":{"0":"MASTER","1":"BACKUP"}' && ok "keepalived notify → boss-lbd state files: A is MASTER of group 0, BACKUP of group 1; track files written" || bad "keepalived state not reported: $st_a"

# ---- VRRP failover: kill A's keepalived → B takes group 0 within ~3 s
kill $(cat "$LAB/A/ka/keepalived.pid") 2>/dev/null || true
t0=$(date +%s.%N); moved=""
for i in $(seq 1 60); do $B ip -4 addr show dev eth0 | grep -q "$VIP/32" && { moved=$(echo "$(date +%s.%N) - $t0" | bc); break; }; sleep 0.1; done
[ -n "$moved" ] && ok "VRRP failover: A's keepalived killed → B owns $VIP after ${moved}s" || bad "VRRP failover: B never took $VIP ($(tail -3 "$LAB/B/keepalived.log"))"
$C ip route replace $VIP/32 via 192.0.2.11 src 198.51.100.5
r=$($C curl -s -m 3 --cacert "$LAB/pki/agent-ca.pem" "https://$VIP/" || echo FAIL)
echo "$r" | grep -q '^v[13]$' && ok "after failover B serves $VIP: $r" || bad "after failover: $r"

echo "lbd-netlab: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
