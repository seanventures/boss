//go:build !linux

package lbd

import (
	"context"
	"errors"
	"net"
)

// GREProber is Linux-only (raw sockets).
type GREProber struct {
	Local net.IP
}

func (g *GREProber) Probe(context.Context, string, string, int) error {
	return errors.New("gre prober: linux only")
}
