//go:build linux

package netpol

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"os"
	"time"

	"golang.org/x/sys/unix"
)

// ServeHelper runs the CAP_NET_ADMIN helper on socket, accepting only
// connections from allowUID. It blocks until ctx ends.
func ServeHelper(ctx context.Context, socket string, allowUID int, m Manager) error {
	_ = os.Remove(socket)
	ln, err := net.Listen("unix", socket)
	if err != nil {
		return err
	}
	if err := os.Chmod(socket, 0o600); err != nil {
		return err
	}
	if err := os.Chown(socket, allowUID, -1); err != nil {
		return fmt.Errorf("chown helper socket: %w", err)
	}
	go func() { <-ctx.Done(); ln.Close() }()
	for {
		c, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			time.Sleep(50 * time.Millisecond)
			continue
		}
		go serveOne(ctx, c.(*net.UnixConn), allowUID, m)
	}
}

func serveOne(ctx context.Context, c *net.UnixConn, allowUID int, m Manager) {
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(30 * time.Second))
	raw, err := c.SyscallConn()
	if err != nil {
		return
	}
	var cred *unix.Ucred
	_ = raw.Control(func(fd uintptr) { cred, _ = unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED) })
	if cred == nil || int(cred.Uid) != allowUID {
		log.Printf("netadm: refused peer uid (want %d)", allowUID)
		return
	}
	line, err := bufio.NewReader(c).ReadBytes('\n')
	if err != nil {
		return
	}
	var req helperReq
	var resp helperResp
	if err := json.Unmarshal(line, &req); err != nil {
		resp = helperResp{Error: "bad request"}
	} else {
		resp = Dispatch(ctx, m, req)
	}
	out, _ := json.Marshal(resp)
	_, _ = c.Write(append(out, '\n'))
}
