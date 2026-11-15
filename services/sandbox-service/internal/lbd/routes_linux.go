//go:build linux

package lbd

import (
	"fmt"
	"net"
	"os"
	"path/filepath"

	"github.com/vishvananda/netlink"
)

// Routes keeps one static route per host (vm_prefix via the host's address),
// the only routing the directors need (LLD §3.7.1). Routes it added that are
// no longer desired are removed; nothing else in the table is touched.
type Routes struct {
	added map[string]string // prefix → gateway
}

func (r *Routes) Apply(hosts []hostapiHost) error {
	if r.added == nil {
		r.added = map[string]string{}
	}
	want := map[string]string{}
	for _, h := range hosts {
		if h.VMPrefix == "" || net.ParseIP(h.Address).To4() == nil {
			continue
		}
		want[h.VMPrefix] = h.Address
	}
	var firstErr error
	for prefix, gw := range r.added {
		if want[prefix] == gw {
			continue
		}
		_, dst, err := net.ParseCIDR(prefix)
		if err == nil {
			if err := netlink.RouteDel(&netlink.Route{Dst: dst, Gw: net.ParseIP(gw)}); err != nil && firstErr == nil {
				firstErr = fmt.Errorf("del route %s: %w", prefix, err)
			}
		}
		delete(r.added, prefix)
	}
	for prefix, gw := range want {
		if r.added[prefix] == gw {
			continue
		}
		_, dst, err := net.ParseCIDR(prefix)
		if err != nil {
			continue
		}
		if err := netlink.RouteReplace(&netlink.Route{Dst: dst, Gw: net.ParseIP(gw)}); err != nil {
			if firstErr == nil {
				firstErr = fmt.Errorf("route %s via %s: %w", prefix, gw, err)
			}
			continue
		}
		r.added[prefix] = gw
	}
	return firstErr
}

// Sysctls prepares the node for IPVS GRE directing and the GRE health check.
//
//	ip_forward=1                 the director forwards (encapsulated) packets
//	vs.expire_nodest_conn=1      deleting a real server resets its connections at once
//	vs.conntrack=0               no conntrack for IPVS flows (DSR: replies never return here)
//	conf.*.rp_filter=0, accept_local=1   SYN-ACKs from a VIP this node holds (health check)
func Sysctls(iface string) error {
	set := func(k, v string) error {
		p := filepath.Join("/proc/sys", k)
		if err := os.WriteFile(p, []byte(v), 0o644); err != nil {
			return fmt.Errorf("sysctl %s=%s: %w", k, v, err)
		}
		return nil
	}
	var firstErr error
	for _, kv := range [][2]string{
		{"net/ipv4/ip_forward", "1"},
		{"net/ipv4/vs/expire_nodest_conn", "1"},
		{"net/ipv4/vs/conntrack", "0"},
		{"net/ipv4/conf/all/rp_filter", "0"},
		{"net/ipv4/conf/default/rp_filter", "0"},
		{"net/ipv4/conf/all/accept_local", "1"},
	} {
		if err := set(kv[0], kv[1]); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	if iface != "" {
		for _, kv := range [][2]string{{"rp_filter", "0"}, {"accept_local", "1"}} {
			if err := set("net/ipv4/conf/"+iface+"/"+kv[0], kv[1]); err != nil && firstErr == nil {
				firstErr = err
			}
		}
	}
	return firstErr
}

// DefaultIface is the interface that carries the given local address.
func DefaultIface(addr string) string {
	ip := net.ParseIP(addr)
	if ip == nil {
		return ""
	}
	links, err := netlink.LinkList()
	if err != nil {
		return ""
	}
	for _, l := range links {
		addrs, err := netlink.AddrList(l, netlink.FAMILY_V4)
		if err != nil {
			continue
		}
		for _, a := range addrs {
			if a.IP.Equal(ip) {
				return l.Attrs().Name
			}
		}
	}
	return ""
}
