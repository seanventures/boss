//go:build linux

package main

import (
	"context"
	"log"

	"github.com/seanheiney/boss/services/sandbox-service/internal/netpol"
)

func inProcessNet() netpol.Manager { return &netpol.Linux{} }

func serveNetadm(ctx context.Context, sock string, uid int) error {
	log.Printf("boss-hostd netadm: serving %s for uid %d", sock, uid)
	return netpol.ServeHelper(ctx, sock, uid, &netpol.Linux{})
}
