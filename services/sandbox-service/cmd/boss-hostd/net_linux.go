//go:build linux

package main

import (
	"context"
	"log"

	"github.com/vishvananda/netlink"

	"github.com/seanventures/boss/services/sandbox-service/internal/netpol"
)

func inProcessNet(p netpol.Policy) netpol.Manager { return &netpol.Linux{Policy: p} }

func serveNetadm(ctx context.Context, sock string, uid int, p netpol.Policy) error {
	// The host's own addresses are internal too: a VM may never reach them.
	if addrs, err := netlink.AddrList(nil, netlink.FAMILY_V4); err == nil {
		for _, a := range addrs {
			p.InternalNets = append(p.InternalNets, a.IP.String()+"/32")
		}
	}
	log.Printf("boss-hostd netadm: serving %s for uid %d, vm_prefix %s, tap owner %d, %d internal nets",
		sock, uid, p.VMPrefix, p.TapOwnerUID, len(p.Internal()))
	return netpol.ServeHelper(ctx, sock, uid, &netpol.Linux{Policy: p})
}
