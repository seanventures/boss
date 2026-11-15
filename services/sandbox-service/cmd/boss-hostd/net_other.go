//go:build !linux

package main

import (
	"context"
	"errors"

	"github.com/seanheiney/boss/services/sandbox-service/internal/netpol"
)

func inProcessNet() netpol.Manager { return netpol.Noop{} }

func serveNetadm(context.Context, string, int) error {
	return errors.New("the netadm helper is Linux-only")
}
