//go:build !linux

package main

import (
	"context"
	"errors"

	"github.com/seanheiney/boss/services/sandbox-service/internal/netpol"
)

func inProcessNet(netpol.Policy) netpol.Manager { return netpol.Noop{} }

func serveNetadm(context.Context, string, int, netpol.Policy) error {
	return errors.New("the netadm helper is Linux-only")
}
