//go:build linux

package netpol

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/exec"
	"strings"

	"github.com/vishvananda/netlink"
	"golang.org/x/sys/unix"
)

// Linux does the privileged work directly. It needs CAP_NET_ADMIN: it runs
// inside the helper (or inside boss-hostd only when that already has it).
type Linux struct {
	Nft string // nft binary (default "nft")
}

func (l *Linux) nft() string {
	if l.Nft != "" {
		return l.Nft
	}
	return "nft"
}

func (l *Linux) run(ctx context.Context, script string) error {
	cmd := exec.CommandContext(ctx, l.nft(), "-f", "-")
	cmd.Stdin = strings.NewReader(script)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("nft: %v: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

func (l *Linux) ensureBase(ctx context.Context) error {
	if err := exec.CommandContext(ctx, l.nft(), "list", "chain", "inet", Table, "pre").Run(); err == nil {
		return nil
	}
	return l.run(ctx, RenderBase())
}

func sysctl(path, val string) error {
	return os.WriteFile(path, []byte(val), 0o644)
}

func (l *Linux) Ensure(ctx context.Context, v VMNet) error {
	if err := v.Validate(); err != nil {
		return err
	}
	if err := l.ensureBase(ctx); err != nil {
		return err
	}
	// Rules before the link exists: the tap is never up without its policy.
	if err := l.run(ctx, RenderVM(v)); err != nil {
		return err
	}
	link, err := netlink.LinkByName(v.Tap)
	if err != nil {
		if err := createTap(v.Tap, v.OwnerUID); err != nil {
			return fmt.Errorf("tap add %s: %w", v.Tap, err)
		}
		if link, err = netlink.LinkByName(v.Tap); err != nil {
			return err
		}
	}
	conf := "/proc/sys/net/ipv4/conf/" + v.Tap + "/"
	if err := sysctl(conf+"proxy_arp", "1"); err != nil {
		return fmt.Errorf("proxy_arp: %w", err)
	}
	_ = sysctl(conf+"rp_filter", "0") // replies are sourced from the VIP
	_ = sysctl("/proc/sys/net/ipv6/conf/"+v.Tap+"/disable_ipv6", "1")
	if err := netlink.LinkSetUp(link); err != nil {
		return fmt.Errorf("tap up: %w", err)
	}
	ip := net.ParseIP(v.VMIP).To4()
	route := &netlink.Route{LinkIndex: link.Attrs().Index, Dst: &net.IPNet{IP: ip, Mask: net.CIDRMask(32, 32)}, Scope: netlink.SCOPE_LINK}
	if err := netlink.RouteReplace(route); err != nil {
		return fmt.Errorf("route %s/32: %w", v.VMIP, err)
	}
	return nil
}

func (l *Linux) Remove(ctx context.Context, tap string) error {
	if !ValidTap(tap) {
		return fmt.Errorf("tap %q is not a boss-hostd tap name", tap)
	}
	if link, err := netlink.LinkByName(tap); err == nil {
		if err := netlink.LinkDel(link); err != nil {
			return fmt.Errorf("tap del: %w", err)
		}
	}
	for _, cmd := range RenderRemove(tap) {
		_ = l.run(ctx, cmd) // missing pieces are fine
	}
	return nil
}

func (l *Linux) Taps(ctx context.Context) ([]string, error) {
	links, err := netlink.LinkList()
	if err != nil {
		return nil, err
	}
	var out []string
	for _, lk := range links {
		if ValidTap(lk.Attrs().Name) {
			out = append(out, lk.Attrs().Name)
		}
	}
	return out, nil
}

func (l *Linux) Counters(ctx context.Context, tap string) (Counters, error) {
	if !ValidTap(tap) {
		return Counters{}, fmt.Errorf("bad tap %q", tap)
	}
	out, err := exec.CommandContext(ctx, l.nft(), "-j", "list", "counters", "table", "inet", Table).Output()
	if err != nil {
		return Counters{}, fmt.Errorf("nft list counters: %w", err)
	}
	var doc struct {
		Nftables []map[string]json.RawMessage `json:"nftables"`
	}
	if err := json.NewDecoder(bytes.NewReader(out)).Decode(&doc); err != nil {
		return Counters{}, err
	}
	var c Counters
	for _, item := range doc.Nftables {
		raw, ok := item["counter"]
		if !ok {
			continue
		}
		var ct struct {
			Name    string `json:"name"`
			Packets uint64 `json:"packets"`
		}
		if json.Unmarshal(raw, &ct) != nil {
			continue
		}
		switch ct.Name {
		case "orig_" + tap:
			c.Originated = ct.Packets
		case "meta_" + tap:
			c.Metadata = ct.Packets
		case "reply_" + tap:
			c.Replies = ct.Packets
		case "in_" + tap:
			c.InboundAllowed = ct.Packets
		case "indrop_" + tap:
			c.InboundDropped = ct.Packets
		}
	}
	return c, nil
}

func (l *Linux) SetDirectors(ctx context.Context, ips []string) error {
	if err := l.ensureBase(ctx); err != nil {
		return err
	}
	s, err := RenderDirectors(ips)
	if err != nil {
		return err
	}
	return l.run(ctx, s)
}

func (l *Linux) PolicyOK(ctx context.Context) (bool, string) {
	var listing strings.Builder
	for _, chain := range []string{"pre", "post"} {
		out, err := exec.CommandContext(ctx, l.nft(), "list", "chain", "inet", Table, chain).CombinedOutput()
		if err != nil {
			return false, "nftables table " + Table + " chain " + chain + " missing"
		}
		listing.Write(out)
	}
	return BaseIntact(listing.String())
}

// createTap makes a persistent, single-queue tap owned by uid, with exactly
// the flags Firecracker opens it with (IFF_TAP|IFF_NO_PI|IFF_VNET_HDR), and
// then closes its own queue so the VMM can attach (a single-queue tap with an
// attached queue is EBUSY).
func createTap(name string, uid int) error {
	fd, err := unix.Open("/dev/net/tun", unix.O_RDWR|unix.O_CLOEXEC, 0)
	if err != nil {
		return err
	}
	defer unix.Close(fd)
	ifr, err := unix.NewIfreq(name)
	if err != nil {
		return err
	}
	ifr.SetUint16(unix.IFF_TAP | unix.IFF_NO_PI | unix.IFF_VNET_HDR)
	if err := unix.IoctlIfreq(fd, unix.TUNSETIFF, ifr); err != nil {
		return fmt.Errorf("TUNSETIFF: %w", err)
	}
	if err := unix.IoctlSetInt(fd, unix.TUNSETOWNER, uid); err != nil {
		return fmt.Errorf("TUNSETOWNER: %w", err)
	}
	if err := unix.IoctlSetInt(fd, unix.TUNSETPERSIST, 1); err != nil {
		return fmt.Errorf("TUNSETPERSIST: %w", err)
	}
	return nil
}
