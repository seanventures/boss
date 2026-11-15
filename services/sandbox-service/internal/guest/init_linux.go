//go:build linux

package guest

import (
	"fmt"
	"net"
	"os"
	"os/signal"
	"syscall"

	"github.com/vishvananda/netlink"
	"golang.org/x/sys/unix"
)

// InitSystem does what PID 1 must before the daemon runs: mounts, loopback
// up, and child reaping. The root disk is the shared read-only base image;
// writable state lives on tmpfs (and the per-VM overlay drive in P8b).
func InitSystem() error {
	mounts := []struct{ src, dst, fstype string }{
		{"proc", "/proc", "proc"},
		{"sysfs", "/sys", "sysfs"},
		{"devtmpfs", "/dev", "devtmpfs"},
		{"tmpfs", "/tmp", "tmpfs"},
		{"tmpfs", "/run", "tmpfs"},
	}
	for _, m := range mounts {
		_ = os.MkdirAll(m.dst, 0o755)
		if err := unix.Mount(m.src, m.dst, m.fstype, 0, ""); err != nil && err != unix.EBUSY {
			fmt.Fprintf(os.Stderr, "boss-vsockd init: mount %s: %v\n", m.dst, err)
		}
	}
	lo, err := netlink.LinkByName("lo")
	if err != nil {
		return fmt.Errorf("lo: %w", err)
	}
	if err := netlink.LinkSetUp(lo); err != nil {
		return fmt.Errorf("lo up: %w", err)
	}
	go reap()
	return nil
}

func reap() {
	ch := make(chan os.Signal, 8)
	signal.Notify(ch, syscall.SIGCHLD)
	for range ch {
		for {
			var ws unix.WaitStatus
			pid, err := unix.Wait4(-1, &ws, unix.WNOHANG, nil)
			if pid <= 0 || err != nil {
				break
			}
		}
	}
}

// ReplicaNetSetup configures eth0 as a routed /32 behind the host's
// proxy-ARP gateway (LLD §3.7.1). The VIP on tunl0 lands with the directors
// in P8b.
func ReplicaNetSetup(b *Bootstrap) error {
	ip := net.ParseIP(b.VMIP).To4()
	gw := net.ParseIP(b.Gateway).To4()
	if ip == nil || gw == nil {
		return fmt.Errorf("replica bootstrap needs vm_ip and gateway")
	}
	link, err := netlink.LinkByName("eth0")
	if err != nil {
		return fmt.Errorf("eth0: %w", err)
	}
	if err := netlink.AddrReplace(link, &netlink.Addr{IPNet: &net.IPNet{IP: ip, Mask: net.CIDRMask(32, 32)}}); err != nil {
		return fmt.Errorf("addr: %w", err)
	}
	if err := netlink.LinkSetUp(link); err != nil {
		return fmt.Errorf("eth0 up: %w", err)
	}
	gwNet := &net.IPNet{IP: gw, Mask: net.CIDRMask(32, 32)}
	if err := netlink.RouteReplace(&netlink.Route{LinkIndex: link.Attrs().Index, Dst: gwNet, Scope: netlink.SCOPE_LINK}); err != nil {
		return fmt.Errorf("gw route: %w", err)
	}
	if err := netlink.RouteReplace(&netlink.Route{LinkIndex: link.Attrs().Index, Gw: gw, Flags: int(netlink.FLAG_ONLINK)}); err != nil {
		return fmt.Errorf("default route: %w", err)
	}
	return nil
}
