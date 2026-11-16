//go:build linux

package e2e

import (
	"context"
	"os"

	"github.com/seanventures/boss/services/sandbox-service/internal/netpol"
)

func netClient() netpol.Manager {
	switch s := os.Getenv("BOSS_E2E_NETADM_SOCKET"); {
	case s == "inprocess": // root hostd (jailer): the policy lives in-process
		return &netpol.Linux{Policy: netpol.Policy{VMPrefix: os.Getenv("BOSS_E2E_VM_PREFIX"), TapOwnerUID: os.Getuid()}}
	case s != "":
		return netpol.HelperClient{Socket: s}
	}
	return netpol.Noop{}
}

func removeTaps(e *env) {
	if e.backend != "firecracker" {
		return
	}
	nc := netClient()
	taps, _ := nc.Taps(context.Background())
	for _, t := range taps {
		_ = nc.Remove(context.Background(), t)
	}
}
