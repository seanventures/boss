//go:build linux

package e2e

import (
	"context"
	"os"

	"github.com/seanheiney/boss/services/sandbox-service/internal/netpol"
)

func netClient() netpol.Manager {
	if s := os.Getenv("BOSS_E2E_NETADM_SOCKET"); s != "" {
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
