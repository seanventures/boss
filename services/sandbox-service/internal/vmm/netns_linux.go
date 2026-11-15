//go:build linux

package vmm

import (
	"errors"
	"fmt"
	"net"
	"os"
	"runtime"

	"github.com/vishvananda/netlink"
	"github.com/vishvananda/netns"
	"golang.org/x/sys/unix"
)

// Every jailed VM gets its own network namespace (LLD §3.18). A per_run VM's
// is empty: nothing to attach a device to. A replica's holds the guest tap
// (GuestTap) bridged to the inner end of a veth pair whose host end carries
// the P8a policy name (bt<12 hex>), so netpol's route, proxy-ARP and nftables
// rules apply unchanged to the host end.

// netnsName is the namespace of a VM (prefix keeps the sweep honest).
func netnsName(fcid string) string { return "bv-" + fcid }

func netnsPath(name string) string { return "/run/netns/" + name }

// createNetns makes an empty named namespace (/run/netns/<name>). Idempotent.
func createNetns(name string) error {
	if _, err := os.Stat(netnsPath(name)); err == nil {
		return nil
	}
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	orig, err := netns.Get()
	if err != nil {
		return err
	}
	defer orig.Close()
	ns, err := netns.NewNamed(name)
	if err != nil {
		return err
	}
	ns.Close()
	return netns.Set(orig)
}

func deleteNetns(name string) error {
	err := netns.DeleteNamed(name)
	if err != nil && errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

// ensureReplicaLink builds the replica's network inside its namespace:
// veth <hostIf> (host) ⇄ <peer> (netns) ─ br0 ─ tap0 (owned by uid, what
// Firecracker opens). Idempotent for an existing host end.
func ensureReplicaLink(nsName, hostIf string, uid int) error {
	if err := createNetns(nsName); err != nil {
		return err
	}
	ns, err := netns.GetFromName(nsName)
	if err != nil {
		return err
	}
	defer ns.Close()
	if _, err := netlink.LinkByName(hostIf); err != nil {
		peer := "vp" + hostIf[2:]
		veth := &netlink.Veth{LinkAttrs: netlink.LinkAttrs{Name: hostIf}, PeerName: peer}
		if err := netlink.LinkAdd(veth); err != nil {
			return fmt.Errorf("veth %s: %w", hostIf, err)
		}
		pl, err := netlink.LinkByName(peer)
		if err != nil {
			return err
		}
		if err := netlink.LinkSetNsFd(pl, int(ns)); err != nil {
			return fmt.Errorf("move %s into %s: %w", peer, nsName, err)
		}
	}
	// The namespace's own stack must stay silent: IPv6 off (MLD/DAD/RS from
	// the bridge or the veth would cross to the host end and count as
	// VM-originated) and no link-local IGMP reports (the bridge's IPv4 stack
	// announces 224.0.0.22 when it comes up).
	if err := inNetns(ns, func() error {
		for _, f := range []string{"all", "default"} {
			if err := os.WriteFile("/proc/sys/net/ipv6/conf/"+f+"/disable_ipv6", []byte("1"), 0o644); err != nil && !os.IsNotExist(err) {
				return err
			}
		}
		return os.WriteFile("/proc/sys/net/ipv4/igmp_link_local_mcast_reports", []byte("0"), 0o644)
	}); err != nil {
		return fmt.Errorf("netns sysctl: %w", err)
	}
	h, err := netlink.NewHandleAt(ns)
	if err != nil {
		return err
	}
	defer h.Close()
	br, err := h.LinkByName("br0")
	if err != nil {
		if err := h.LinkAdd(&netlink.Bridge{LinkAttrs: netlink.LinkAttrs{Name: "br0"}}); err != nil {
			return fmt.Errorf("bridge: %w", err)
		}
		if br, err = h.LinkByName("br0"); err != nil {
			return err
		}
	}
	if _, err := h.LinkByName(GuestTap); err != nil {
		if err := createTapIn(ns, GuestTap, uid); err != nil {
			return fmt.Errorf("tap in %s: %w", nsName, err)
		}
	}
	for _, name := range []string{GuestTap, "vp" + hostIf[2:]} {
		l, err := h.LinkByName(name)
		if err != nil {
			return err
		}
		if l.Attrs().MasterIndex != br.Attrs().Index {
			if err := h.LinkSetMaster(l, br); err != nil {
				return fmt.Errorf("enslave %s: %w", name, err)
			}
		}
		if err := h.LinkSetUp(l); err != nil {
			return fmt.Errorf("%s up: %w", name, err)
		}
	}
	if err := h.LinkSetUp(br); err != nil {
		return fmt.Errorf("br0 up: %w", err)
	}
	return nil
}

// inNetns runs fn with the calling thread switched into ns (sysctls under
// /proc/sys/net and TUNSETIFF act on the thread's namespace).
func inNetns(ns netns.NsHandle, fn func() error) error {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	orig, err := netns.Get()
	if err != nil {
		return err
	}
	defer orig.Close()
	if err := netns.Set(ns); err != nil {
		return err
	}
	defer func() { _ = netns.Set(orig) }()
	return fn()
}

// createTapIn makes a persistent single-queue tap inside ns, owned by uid,
// with the flags Firecracker opens it with, then releases its own queue.
func createTapIn(ns netns.NsHandle, name string, uid int) error {
	return inNetns(ns, func() error { return createTap(name, uid) })
}

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

// hostLinkExists reports whether the policy-side link is present.
func hostLinkExists(name string) bool {
	_, err := net.InterfaceByName(name)
	return err == nil
}
