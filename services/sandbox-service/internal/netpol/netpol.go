// Package netpol is replica networking (LLD §3.7): one tap, one routed /32
// and one MAC per replica VM, with inbound-only nftables. per_run VMs get no
// network device at all, so nothing here runs for them.
//
// Policy per tap (table inet boss_hostd, default DROP + counted):
//
//	IN  (to the VM):   IPIP from a director IP to vm_ip            → accept (counted in_)
//	                   everything else                              → drop   (counted indrop_)
//	OUT (from the VM): to 169.254.169.254                           → drop   (counted meta_)
//	                   src VIP, sport ∈ vip_ports, not a bare SYN,
//	                   dst ∈ peers learned from inbound IPIP        → accept (counted reply_)
//	                   everything else (DNS, VM→VM, internet, host) → drop   (counted orig_)
//
// The orig_ and meta_ counters are the P8a-T4 evidence: a VM that never
// originates a packet leaves them at zero.
//
// Why not "ct state established" (as the LLD sketches): the host only sees the
// outer IPIP flow, so its conntrack has no entry for the inner TCP flow and the
// reply would never match. Instead, every inbound IPIP packet records its inner
// source address in a per-tap dynamic set (peers_, 10 min timeout), and a reply
// is allowed only back to such a peer. See docs/infra/HOST_API.md §Networking.
//
// All privileged work goes through the Manager interface. In production the
// implementation is a small CAP_NET_ADMIN helper (helper.go) reached over a
// Unix socket, so boss-hostd itself runs unprivileged.
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

// RenderBase creates the table, the director set, the dispatch maps and the
// fail-closed catch-all for any bt* tap with no per-VM chain.
func RenderBase() string {
	return `table inet ` + Table + ` {
	set directors { type ipv4_addr; }
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

// RenderVM is one replica's chains, counters and peer set, as an nft -f
// script that is safe to re-apply (chains are flushed, counters kept).
func RenderVM(v VMNet) string {
	t := v.Tap
	tb := "inet " + Table
	var b strings.Builder
	w := func(format string, a ...any) { fmt.Fprintf(&b, format+"\n", a...) }
	for _, c := range []string{"orig_", "meta_", "reply_", "in_", "indrop_"} {
		w("add counter %s %s%s", tb, c, t)
	}
	w("add set %s peers_%s { typeof @nh,256,32; flags dynamic,timeout; timeout 10m; }", tb, t)
	w("add chain %s out_%s", tb, t)
	w("add chain %s in_%s", tb, t)
	w("flush chain %s out_%s", tb, t)
	w("flush chain %s in_%s", tb, t)
	// OUT: from the VM.
	w("add rule %s out_%s ip daddr 169.254.169.254 counter name meta_%s drop", tb, t, t)
	if v.VIP != "" && len(v.VIPPorts) > 0 {
		w("add rule %s out_%s ip saddr %s tcp sport %s tcp flags & (syn|ack) != syn @nh,128,32 @peers_%s counter name reply_%s accept",
			tb, t, v.VIP, portSet(v.VIPPorts), t, t)
	}
	w("add rule %s out_%s counter name orig_%s drop", tb, t, t)
	// IN: to the VM. Learn the inner (caller) source of TCP-in-IPIP.
	w("add rule %s in_%s ip saddr @directors ip daddr %s meta l4proto 4 @nh,232,8 6 update @peers_%s { @nh,256,32 } counter name in_%s accept",
		tb, t, v.VMIP, t, t)
	w("add rule %s in_%s ip saddr @directors ip daddr %s meta l4proto 4 counter name in_%s accept", tb, t, v.VMIP, t)
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
		fmt.Sprintf("delete set %s peers_%s", tb, tap),
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
