//go:build linux

package guest

import (
	"fmt"
	"log"
	"net"
	"os"
	"os/signal"
	"syscall"

	"github.com/vishvananda/netlink"
	"golang.org/x/sys/unix"
)

// OverlayDevice is the per-VM writable overlay drive (the second virtio
// block device; the first is the shared read-only base rootfs).
const OverlayDevice = "/dev/vdb"

// InitSystem does what PID 1 must before the daemon runs: mounts, the
// writable overlay root when an overlay drive is attached, loopback up, and
// child reaping. The root disk is the shared read-only base image; without an
// overlay drive writable state lives on tmpfs.
func InitSystem() error {
	_ = os.MkdirAll("/dev", 0o755)
	if err := unix.Mount("devtmpfs", "/dev", "devtmpfs", 0, ""); err != nil && err != unix.EBUSY {
		fmt.Fprintf(os.Stderr, "boss-vsockd init: mount /dev: %v\n", err)
	}
	if err := switchToOverlay(); err != nil {
		fmt.Fprintf(os.Stderr, "boss-vsockd init: overlay root: %v (staying on the read-only root)\n", err)
	}
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

// switchToOverlay mounts the overlay drive and pivots into an overlayfs root
// (lower = the read-only base, upper = the drive), so every path is writable
// and nothing is ever written to the shared base (LLD §3.4). No drive: no-op.
func switchToOverlay() error {
	if _, err := os.Stat(OverlayDevice); err != nil {
		return nil
	}
	if err := unix.Mount("tmpfs", "/run", "tmpfs", 0, ""); err != nil && err != unix.EBUSY {
		return fmt.Errorf("tmpfs /run: %w", err)
	}
	for _, d := range []string{"/run/ov", "/run/newroot"} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return err
		}
	}
	if err := unix.Mount(OverlayDevice, "/run/ov", "ext4", 0, ""); err != nil {
		return fmt.Errorf("mount overlay drive: %w", err)
	}
	for _, d := range []string{"/run/ov/upper", "/run/ov/work"} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return err
		}
	}
	opts := "lowerdir=/,upperdir=/run/ov/upper,workdir=/run/ov/work"
	if err := unix.Mount("overlay", "/run/newroot", "overlay", 0, opts); err != nil {
		return fmt.Errorf("mount overlayfs: %w", err)
	}
	if err := os.MkdirAll("/run/newroot/oldroot", 0o755); err != nil {
		return err
	}
	if err := os.Chdir("/run/newroot"); err != nil {
		return err
	}
	if err := unix.PivotRoot(".", "oldroot"); err != nil {
		return fmt.Errorf("pivot_root: %w", err)
	}
	if err := os.Chdir("/"); err != nil {
		return err
	}
	// The overlay holds its own references to the lower and upper mounts.
	_ = unix.Unmount("/oldroot", unix.MNT_DETACH)
	_ = os.Remove("/oldroot")
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
// proxy-ARP gateway (LLD §3.7.1). A restored clone first takes its own MAC
// from the bootstrap (the snapshot's NIC carries the template's). The VIP on
// tunl0 lands with the directors.
func ReplicaNetSetup(b *Bootstrap) error {
	ip := net.ParseIP(b.VMIP).To4()
	gw := net.ParseIP(b.Gateway).To4()
	if ip == nil || gw == nil {
		return fmt.Errorf("replica bootstrap needs vm_ip and gateway")
	}
	// The VIP lives on this VM too (P8b, tunl0/lo): never answer ARP for it,
	// and always ARP from eth0's own address, so the host's proxy-ARP (which
	// routes only vm_ip/32 to this tap) answers for the gateway (LLD §3.7.1).
	for _, kv := range [][2]string{{"all/arp_ignore", "1"}, {"all/arp_announce", "2"},
		{"eth0/arp_ignore", "1"}, {"eth0/arp_announce", "2"}, {"all/rp_filter", "0"}} {
		_ = os.WriteFile("/proc/sys/net/ipv4/conf/"+kv[0], []byte(kv[1]), 0o644)
	}
	// boss-runtime's TLS listener on VIP:443 is internet-facing (LLD §3.17.1): SYN cookies on
	// and a deep accept queue, so a SYN flood is absorbed by the kernel, not the listener
	// (runtime/listener.py listens with a 4096 backlog and checks tcp_syncookies at start).
	for _, kv := range [][2]string{{"ipv4/tcp_syncookies", "1"},
		{"ipv4/tcp_max_syn_backlog", "4096"}, {"core/somaxconn", "4096"}} {
		_ = os.WriteFile("/proc/sys/net/"+kv[0], []byte(kv[1]), 0o644)
	}
	link, err := netlink.LinkByName("eth0")
	if err != nil {
		return fmt.Errorf("eth0: %w", err)
	}
	if mac, err := net.ParseMAC(b.MAC); err == nil && link.Attrs().HardwareAddr.String() != mac.String() {
		_ = netlink.LinkSetDown(link)
		if err := netlink.LinkSetHardwareAddr(link, mac); err != nil {
			log.Printf("boss-vsockd: eth0 mac %s: %v", mac, err)
		}
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
	if b.VIP != "" {
		if err := replicaVIPSetup(ip, b.VIP); err != nil {
			// The VM still serves the internal pull loop without its VIP; the
			// directors' health check keeps it out of the VIP until this works.
			log.Printf("boss-vsockd: vip %s: %v", b.VIP, err)
		}
	}
	return nil
}

// GREDevice is the guest's decapsulating tunnel (P8b-10, HOST_API.md §9).
const GREDevice = "bgre0"

// replicaVIPSetup makes the VM a GRE-mode IPVS real server (LLD §3.7.1,
// amended to GRE): the VIP on lo (never announced: arp_ignore=1 and
// arp_announce=2 are set above) and a GRE device with local = vm_ip and no
// fixed remote, so encapsulated callers from either director decapsulate to
// the VIP. Replies leave eth0 with source = VIP (DSR); the host's nftables
// let them out only on a flow it learned from the caller's SYN.
func replicaVIPSetup(vmIP net.IP, vip string) error {
	v := net.ParseIP(vip).To4()
	if v == nil {
		return fmt.Errorf("vip %q is not IPv4", vip)
	}
	lo, err := netlink.LinkByName("lo")
	if err != nil {
		return fmt.Errorf("lo: %w", err)
	}
	if err := netlink.AddrReplace(lo, &netlink.Addr{IPNet: &net.IPNet{IP: v, Mask: net.CIDRMask(32, 32)}}); err != nil {
		return fmt.Errorf("vip on lo: %w", err)
	}
	gre, err := netlink.LinkByName(GREDevice)
	if err != nil {
		gre = &netlink.Gretun{LinkAttrs: netlink.LinkAttrs{Name: GREDevice}, Local: vmIP, Ttl: 64}
		if err := netlink.LinkAdd(gre); err != nil {
			return fmt.Errorf("gre device (ip_gre in the guest kernel?): %w", err)
		}
	}
	if err := netlink.LinkSetUp(gre); err != nil {
		return fmt.Errorf("gre up: %w", err)
	}
	_ = os.WriteFile("/proc/sys/net/ipv4/conf/"+GREDevice+"/rp_filter", []byte("0"), 0o644)
	return nil
}
