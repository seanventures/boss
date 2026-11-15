package lbd

import (
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
)

// GroupSize is how many VIPs share one VRRP instance (LLD §3.14.2 "about 50").
const GroupSize = 50

// MaxGroups: VRIDs are 1..255; group i uses VRID VRIDBase+i.
const (
	VRIDBase  = 1
	MaxGroups = 255 - VRIDBase
)

// Group computes the VRRP grouping both directors must agree on: VIPs sorted,
// chunked by GroupSize; group i is MASTER on directors[i%2] (ids sorted), so
// half the groups live on A and half on B. The control plane runs this and
// sends the result; boss-lbd uses it for a desired file in lab mode.
func Group(vips []string, directorIDs []string) []hostapiGroup {
	sorted := uniqueSorted(vips)
	ids := uniqueSorted(directorIDs)
	var out []hostapiGroup
	for i := 0; i*GroupSize < len(sorted) && i < MaxGroups; i++ {
		end := (i + 1) * GroupSize
		if end > len(sorted) {
			end = len(sorted)
		}
		g := hostapiGroup{Group: i, VRID: VRIDBase + i, VIPs: sorted[i*GroupSize : end]}
		if len(ids) > 0 {
			g.Master = ids[i%len(ids)]
		}
		out = append(out, g)
	}
	return out
}

func uniqueSorted(in []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, s := range in {
		if s == "" || seen[s] {
			continue
		}
		seen[s] = true
		out = append(out, s)
	}
	sort.Strings(out)
	return out
}

// KeepalivedConfig is what the rendered keepalived.conf needs.
type KeepalivedConfig struct {
	SelfID      string
	SelfAddress string
	PeerAddress string // "" = no peer yet (single director: MASTER of everything)
	Iface       string
	Groups      []hostapiGroup
	// TrackBin is boss-lbd's own path: `<bin> track --group N` is the VRRP
	// track script (lease), `<bin> notify` the state notifier.
	TrackBin string
	StateDir string
	// Password is unused: VRRP auth is deprecated in keepalived 2.x; unicast
	// peers plus the lease carry the trust.
}

// RenderKeepalived writes the VRRP-only configuration: no virtual_server
// blocks ever (D3: boss-lbd owns IPVS). Both sides are `state BACKUP` with
// `nopreempt` and different priorities, which is how keepalived wants
// non-preemption expressed; the higher priority wins the first election.
func RenderKeepalived(c KeepalivedConfig) string {
	var b strings.Builder
	fmt.Fprintf(&b, "# boss-lbd: VRRP only. IPVS is programmed by boss-lbd, never by keepalived (D3).\n")
	fmt.Fprintf(&b, "global_defs {\n  router_id %s\n  enable_script_security\n  script_user root\n  vrrp_garp_master_refresh 60\n}\n\n", c.SelfID)
	for _, g := range c.Groups {
		fmt.Fprintf(&b, "vrrp_script boss_lease_g%d {\n  script \"%s track --group %d --state-dir %s\"\n  interval 1\n  timeout 2\n  fall 2\n  rise 2\n}\n\n",
			g.Group, c.TrackBin, g.Group, c.StateDir)
	}
	for _, g := range c.Groups {
		prio := 100
		if g.Master == c.SelfID || c.PeerAddress == "" {
			prio = 150
		}
		fmt.Fprintf(&b, "vrrp_instance boss_g%d {\n  state BACKUP\n  nopreempt\n  interface %s\n  virtual_router_id %d\n  priority %d\n  advert_int 1\n",
			g.Group, c.Iface, g.VRID, prio)
		if c.PeerAddress != "" {
			fmt.Fprintf(&b, "  unicast_src_ip %s\n  unicast_peer {\n    %s\n  }\n", c.SelfAddress, c.PeerAddress)
		}
		fmt.Fprintf(&b, "  track_script {\n    boss_lease_g%d\n  }\n", g.Group)
		if len(g.VIPs) > 0 {
			fmt.Fprintf(&b, "  virtual_ipaddress {\n    %s/32 dev %s\n  }\n", g.VIPs[0], c.Iface)
		}
		if len(g.VIPs) > 1 {
			fmt.Fprintf(&b, "  virtual_ipaddress_excluded {\n")
			for _, v := range g.VIPs[1:] {
				fmt.Fprintf(&b, "    %s/32 dev %s\n", v, c.Iface)
			}
			fmt.Fprintf(&b, "  }\n")
		}
		for _, st := range []string{"master", "backup", "fault", "stop"} {
			fmt.Fprintf(&b, "  notify_%s \"%s notify --group %d --state-dir %s --to %s\"\n", st, c.TrackBin, g.Group, c.StateDir, strings.ToUpper(st))
		}
		fmt.Fprintf(&b, "}\n\n")
	}
	return b.String()
}

// WriteIfChanged writes content to path (0600) and reports whether it changed.
func WriteIfChanged(path, content string) (bool, error) {
	if cur, err := os.ReadFile(path); err == nil && string(cur) == content {
		return false, nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return false, err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(content), 0o600); err != nil {
		return false, err
	}
	return true, os.Rename(tmp, path)
}

// ReloadKeepalived sends SIGHUP to the pid in pidFile (keepalived re-reads
// its configuration). A missing pid file is not an error: keepalived may not
// be running yet, and it reads the file at start.
func ReloadKeepalived(pidFile string) error {
	raw, err := os.ReadFile(pidFile)
	if err != nil {
		return nil
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
	if err != nil || pid <= 0 {
		return fmt.Errorf("keepalived pid file %s: %q", pidFile, strings.TrimSpace(string(raw)))
	}
	return syscall.Kill(pid, syscall.SIGHUP)
}

// KeepalivedLive reports whether the pid in pidFile is alive.
func KeepalivedLive(pidFile string) bool {
	raw, err := os.ReadFile(pidFile)
	if err != nil {
		return false
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
	if err != nil || pid <= 0 {
		return false
	}
	return syscall.Kill(pid, 0) == nil
}

// ValidVIP is an IPv4 /32 string.
func ValidVIP(s string) bool { return net.ParseIP(s).To4() != nil }
