// Package netpol is replica networking (LLD §3.7): one tap, one routed /32
// and one MAC per replica VM, with inbound-only nftables. per_run VMs get no
// network device at all, so nothing here runs for them.
//
// Policy per tap (table inet boss_hostd, default DROP + counted):
//
//	IN  (to the VM):   GRE from a director IP to vm_ip, inner TCP to VIP:vip_port → accept (in_);
//	                     a SYN learns the flow (VIP, caller, vip_port, caller_port),
//	                     later packets refresh only a live flow
//	                   everything else                                     → drop   (indrop_)
//	OUT (from the VM): to 169.254.169.254                                  → drop   (meta_)
//	                   a health-check reply to a director on a flow it opened → accept (reply_)
//	                   to any internal net (VM pool, host, infra, link-local) → drop (orig_)
//	                   a reply on a learned flow (not a bare SYN)          → accept (reply_)
//	                     RST deletes the flow; FIN moves it to closing_ (30 s)
//	                   everything else (DNS, VM→VM, internet, host)        → drop   (orig_)
//
// The orig_ and meta_ counters are the P8a-T4 evidence: a VM that never
// originates a packet leaves them at zero.
//
// Why GRE and a learned flow set, not IPIP and "ct state established" (the
// LLD sketch): with IPIP the host sees only the outer flow, so its conntrack
// has no entry for the inner TCP flow, and nftables cannot type a raw-payload
// concatenation of the inner IPIP header. nftables CAN match GRE's inner
// headers with types ("gre ip saddr", "gre tcp sport"), so the host learns the
// exact 4-tuple of every inbound caller flow and allows a reply only on it.
// IPVS speaks GRE tunnels (ipvsadm --tun-type gre). Proven on boss-1 in
// scripts/sandbox/hostd-netlab.sh. See docs/infra/HOST_API.md §5.
//
// All privileged work goes through the Manager interface. In production the
// implementation is a small CAP_NET_ADMIN helper (helper.go) reached over a
// Unix socket; the helper's own root-owned flags (Policy) decide the VM
// prefix, the tap owner and the internal nets — never the caller.
package netpol

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// Table is the nftables table boss-hostd owns.
const Table = "boss_hostd"

// TapPrefix marks every tap boss-hostd creates (netlink scan key).
const TapPrefix = "bt"

// Gateway is the guest's next hop, answered by proxy-ARP on the tap.
const Gateway = "169.254.1.1"

var tapRE = regexp.MustCompile(`^bt[0-9a-f]{12}$`)

// TapName is a stable tap name for a VM (IFNAMSIZ-safe, scan-friendly).
func TapName(vmID string) string {
	sum := sha256.Sum256([]byte(vmID))
	return TapPrefix + hex.EncodeToString(sum[:])[:12]
}

// ValidTap reports whether name is a boss-hostd tap.
func ValidTap(name string) bool { return tapRE.MatchString(name) }

// MACFor derives a stable, locally administered MAC from the VM's IPv4.
func MACFor(vmIP string) string {
	ip := net.ParseIP(vmIP).To4()
	if ip == nil {
		return ""
	}
	return fmt.Sprintf("02:fc:%02x:%02x:%02x:%02x", ip[0], ip[1], ip[2], ip[3])
}

// VMNet is one replica's network attachment.
type VMNet struct {
	VMID     string `json:"vm_id"`
	Tap      string `json:"tap"`
	VMIP     string `json:"vm_ip"`
	VIP      string `json:"vip,omitempty"`
	VIPPorts []int  `json:"vip_ports,omitempty"`
	OwnerUID int    `json:"owner_uid"`
	VMPrefix string `json:"vm_prefix"` // the host's block; vm_ip must be inside it
}

// Validate is the helper boundary's input check: nothing outside these shapes
// reaches netlink or nft.
func (v VMNet) Validate() error {
	if !ValidTap(v.Tap) {
		return fmt.Errorf("tap %q is not a boss-hostd tap name", v.Tap)
	}
	ip := net.ParseIP(v.VMIP).To4()
	if ip == nil {
		return fmt.Errorf("vm_ip %q is not IPv4", v.VMIP)
	}
	_, prefix, err := net.ParseCIDR(v.VMPrefix)
	if err != nil || !prefix.Contains(ip) {
		return fmt.Errorf("vm_ip %s is not inside the host vm_prefix %q", v.VMIP, v.VMPrefix)
	}
	if v.VIP != "" && net.ParseIP(v.VIP).To4() == nil {
		return fmt.Errorf("vip %q is not IPv4", v.VIP)
	}
	for _, p := range v.VIPPorts {
		if p <= 0 || p > 65535 {
			return fmt.Errorf("vip port %d out of range", p)
		}
	}
	if v.OwnerUID < 0 {
		return fmt.Errorf("owner uid %d invalid", v.OwnerUID)
	}
	return nil
}

// Policy is the helper's own, root-owned configuration (boss-hostd netadm
// flags). The helper never takes these from its caller.
type Policy struct {
	VMPrefix     string   // the host's VM block; every vm_ip must be inside it
	TapOwnerUID  int      // the only owner a tap may get (boss-hostd's uid)
	InternalNets []string // CIDRs no VM packet may reach (plus DefaultInternalNets and the VM prefix)
}

// Check validates a request against the policy and fills in what the policy
// decides. A caller value that disagrees with the policy is refused.
func (p Policy) Check(v *VMNet) error {
	if p.VMPrefix == "" {
		return fmt.Errorf("helper has no --vm-prefix configured")
	}
	if v.VMPrefix != "" && v.VMPrefix != p.VMPrefix {
		return fmt.Errorf("vm_prefix %q is not this host's (%s)", v.VMPrefix, p.VMPrefix)
	}
	if v.OwnerUID != p.TapOwnerUID {
		return fmt.Errorf("tap owner uid %d refused (helper allows %d)", v.OwnerUID, p.TapOwnerUID)
	}
	v.VMPrefix = p.VMPrefix
	return v.Validate()
}

// Internal is the full internal-net list for the base table.
func (p Policy) Internal() []string {
	seen := map[string]bool{}
	var out []string
	for _, n := range append(append(append([]string{}, DefaultInternalNets...), p.InternalNets...), p.VMPrefix) {
		if _, _, err := net.ParseCIDR(n); err == nil && !seen[n] {
			seen[n] = true
			out = append(out, n)
		}
	}
	return out
}

// Counters are one tap's nftables counters (packets).
type Counters struct {
	Originated     uint64 `json:"originated"` // orig_: VM-originated, dropped
	Metadata       uint64 `json:"metadata"`   // meta_: to 169.254.169.254, dropped
	Replies        uint64 `json:"replies"`    // reply_: allowed replies
	InboundAllowed uint64 `json:"inbound_allowed"`
	InboundDropped uint64 `json:"inbound_dropped"`
}

// Manager is the privileged network surface.
type Manager interface {
	// Ensure creates or repairs the tap, route, proxy-ARP and rules. Idempotent.
	Ensure(ctx context.Context, v VMNet) error
	// Remove deletes the tap and its rules. Missing pieces are not an error.
	Remove(ctx context.Context, tap string) error
	// Taps lists boss-hostd taps that exist (netlink scan).
	Taps(ctx context.Context) ([]string, error)
	Counters(ctx context.Context, tap string) (Counters, error)
	SetDirectors(ctx context.Context, ips []string) error
	// PolicyOK reports whether the base table and its fail-closed rules exist.
	PolicyOK(ctx context.Context) (bool, string)
}

// ---- nft rendering (pure; unit-tested on any OS) -------------------------

// DefaultInternalNets are always "internal": nothing a VM sends may reach
// them, reply or not (loopback; link-local incl. metadata). The VM prefix,
// the host's own addresses and the operator's infra ranges (--internal-nets)
// are added by the helper. Private ranges are NOT internal by default:
// callers inside a private network are a supported case.
var DefaultInternalNets = []string{"127.0.0.0/8", "169.254.0.0/16"}

// RenderBase creates the table, the director and internal-net sets, the
// dispatch maps and the fail-closed catch-all for any bt* tap with no chain.
func RenderBase(internal []string) string {
	elems := ""
	if len(internal) > 0 {
		elems = " elements = { " + strings.Join(internal, ", ") + " };"
	}
	return `table inet ` + Table + ` {
	set directors { type ipv4_addr; }
	set internal_nets { type ipv4_addr; flags interval; auto-merge;` + elems + ` }
	map from_tap { type ifname : verdict; }
	map to_tap { type ifname : verdict; }
	counter drop_unknown_tap { }
	chain pre {
		type filter hook prerouting priority -300; policy accept;
		iifname vmap @from_tap
		iifname "` + TapPrefix + `*" counter name drop_unknown_tap drop
	}
	chain post {
		type filter hook postrouting priority 0; policy accept;
		oifname vmap @to_tap
		oifname "` + TapPrefix + `*" counter name drop_unknown_tap drop
	}
}
`
}

func portSet(ports []int) string {
	ps := append([]int(nil), ports...)
	sort.Ints(ps)
	s := make([]string, len(ps))
	for i, p := range ps {
		s[i] = strconv.Itoa(p)
	}
	return "{ " + strings.Join(s, ", ") + " }"
}

// RenderVM is one replica's chains, counters and flow set, as an nft -f
// script that is safe to re-apply (chains are flushed, counters and learned
// flows kept).
func RenderVM(v VMNet) string {
	t := v.Tap
	tb := "inet " + Table
	var b strings.Builder
	w := func(format string, a ...any) { fmt.Fprintf(&b, format+"\n", a...) }
	for _, c := range []string{"orig_", "meta_", "reply_", "in_", "indrop_"} {
		w("add counter %s %s%s", tb, c, t)
	}
	w("add set %s flows_%s { type ipv4_addr . ipv4_addr . inet_service . inet_service; flags dynamic,timeout; timeout 10m; }", tb, t)
	w("add set %s closing_%s { type ipv4_addr . ipv4_addr . inet_service . inet_service; flags dynamic,timeout; timeout 30s; }", tb, t)
	w("add chain %s out_%s", tb, t)
	w("add chain %s in_%s", tb, t)
	w("flush chain %s out_%s", tb, t)
	w("flush chain %s in_%s", tb, t)
	tuple := "ip saddr . ip daddr . tcp sport . tcp dport"
	inner := "gre ip daddr . gre ip saddr . gre tcp dport . gre tcp sport" // reply orientation
	// OUT: from the VM.
	w("add rule %s out_%s ip daddr 169.254.169.254 counter name meta_%s drop", tb, t, t)
	if v.VIP != "" && len(v.VIPPorts) > 0 {
		// The directors' health check (P8b-10): a SYN inside GRE from a director's own
		// address, answered by the VM's SYN-ACK to that address. Directors sit on the
		// infra network, which is internal, so this ONE reply shape is allowed before
		// the internal-net drop: non-SYN, from the VIP, to a director, on a flow that
		// director opened (learned from its GRE SYN). Nothing else internal ever is.
		w("add rule %s out_%s ip saddr %s tcp sport %s ip daddr @directors tcp flags & (syn|ack) != syn %s @flows_%s counter name reply_%s accept",
			tb, t, v.VIP, portSet(v.VIPPorts), tuple, t, t)
	}
	w("add rule %s out_%s ip daddr @internal_nets counter name orig_%s drop", tb, t, t)
	if v.VIP != "" && len(v.VIPPorts) > 0 {
		reply := fmt.Sprintf("ip saddr %s tcp sport %s", v.VIP, portSet(v.VIPPorts))
		// RST ends the flow; FIN moves it to closing_ (30 s) for the last ACKs.
		w("add rule %s out_%s %s tcp flags & rst == rst %s @flows_%s delete @flows_%s { %s } counter name reply_%s accept",
			tb, t, reply, tuple, t, t, tuple, t)
		w("add rule %s out_%s %s tcp flags & fin == fin %s @flows_%s delete @flows_%s { %s } add @closing_%s { %s } counter name reply_%s accept",
			tb, t, reply, tuple, t, t, tuple, t, tuple, t)
		w("add rule %s out_%s %s tcp flags & (syn|ack) != syn %s @flows_%s counter name reply_%s accept",
			tb, t, reply, tuple, t, t)
		w("add rule %s out_%s %s tcp flags & (syn|ack) != syn %s @closing_%s counter name reply_%s accept",
			tb, t, reply, tuple, t, t)
	}
	w("add rule %s out_%s counter name orig_%s drop", tb, t, t)
	// IN: to the VM, GRE from a director to vm_ip carrying TCP to VIP:port.
	// A caller's SYN opens the flow; later packets refresh only a live flow
	// (so a closing flow is never re-learned).
	if v.VIP != "" && len(v.VIPPorts) > 0 {
		gre := fmt.Sprintf("ip saddr @directors ip daddr %s gre ip daddr %s gre tcp dport %s", v.VMIP, v.VIP, portSet(v.VIPPorts))
		w("add rule %s in_%s %s gre tcp flags & (syn|ack) == syn update @flows_%s { %s } counter name in_%s accept",
			tb, t, gre, t, inner, t)
		w("add rule %s in_%s %s %s @flows_%s update @flows_%s { %s } counter name in_%s accept",
			tb, t, gre, inner, t, t, inner, t)
		w("add rule %s in_%s %s counter name in_%s accept", tb, t, gre, t)
	}
	w("add rule %s in_%s counter name indrop_%s drop", tb, t, t)
	w("add element %s from_tap { \"%s\" : jump out_%s }", tb, t, t)
	w("add element %s to_tap { \"%s\" : jump in_%s }", tb, t, t)
	return b.String()
}

// RenderRemove lists the per-VM teardown commands, one per line, each run on
// its own so a missing piece does not abort the rest.
func RenderRemove(tap string) []string {
	tb := "inet " + Table
	return []string{
		fmt.Sprintf("delete element %s from_tap { \"%s\" }", tb, tap),
		fmt.Sprintf("delete element %s to_tap { \"%s\" }", tb, tap),
		fmt.Sprintf("flush chain %s out_%s", tb, tap),
		fmt.Sprintf("flush chain %s in_%s", tb, tap),
		fmt.Sprintf("delete chain %s out_%s", tb, tap),
		fmt.Sprintf("delete chain %s in_%s", tb, tap),
		fmt.Sprintf("delete set %s flows_%s", tb, tap),
		fmt.Sprintf("delete set %s closing_%s", tb, tap),
		fmt.Sprintf("delete counter %s orig_%s", tb, tap),
		fmt.Sprintf("delete counter %s meta_%s", tb, tap),
		fmt.Sprintf("delete counter %s reply_%s", tb, tap),
		fmt.Sprintf("delete counter %s in_%s", tb, tap),
		fmt.Sprintf("delete counter %s indrop_%s", tb, tap),
	}
}

// RenderDirectors replaces the director set.
func RenderDirectors(ips []string) (string, error) {
	var good []string
	for _, ip := range ips {
		if net.ParseIP(ip).To4() == nil {
			return "", fmt.Errorf("director %q is not IPv4", ip)
		}
		good = append(good, ip)
	}
	s := fmt.Sprintf("flush set inet %s directors\n", Table)
	if len(good) > 0 {
		s += fmt.Sprintf("add element inet %s directors { %s }\n", Table, strings.Join(good, ", "))
	}
	return s, nil
}

// BaseIntact checks an `nft list chain` listing of the pre and post chains:
// both dispatch maps and both fail-closed catch-alls must be present (nft
// prints the counter name quoted; the check ignores quoting).
func BaseIntact(listing string) (bool, string) {
	flat := strings.Join(strings.Fields(strings.ReplaceAll(listing, `"`, "")), " ")
	for _, want := range []string{
		"iifname vmap @from_tap",
		"iifname " + TapPrefix + "* counter name drop_unknown_tap drop",
		"oifname vmap @to_tap",
		"oifname " + TapPrefix + "* counter name drop_unknown_tap drop",
	} {
		if !strings.Contains(flat, want) {
			return false, "nftables policy drift: missing \"" + want + "\""
		}
	}
	return true, ""
}

// ---- Noop (process backend) ----------------------------------------------

// Noop is used by the process backend: there are no taps, and it says so.
type Noop struct{}

func (Noop) Ensure(context.Context, VMNet) error                { return nil }
func (Noop) Remove(context.Context, string) error               { return nil }
func (Noop) Taps(context.Context) ([]string, error)             { return nil, nil }
func (Noop) Counters(context.Context, string) (Counters, error) { return Counters{}, nil }
func (Noop) SetDirectors(context.Context, []string) error       { return nil }
func (Noop) PolicyOK(context.Context) (bool, string) {
	return false, "process backend: no network isolation (no taps, no nftables)"
}
