package guest

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"sync"
	"time"
)

// Transport is how the guest reaches the host: vsock in a VM, Unix sockets
// in a VM directory for the process backend.
type Transport interface {
	ListenControl() (net.Listener, error)
	ListenInbox() (net.Listener, error)
	DialHost(port int) (net.Conn, error)
}

// UnixTransport is the process-backend transport. The host listens on
// <Dir>/g_<port>.sock (one set per VM, so the socket identifies the VM) and
// the guest serves control on <Dir>/ctl.sock.
type UnixTransport struct{ Dir string }

func UnixControlPath(dir string) string { return filepath.Join(dir, "ctl.sock") }
func UnixInboxPath(dir string) string   { return filepath.Join(dir, "w_7105.sock") }
func UnixGuestPath(dir string, port int) string {
	return filepath.Join(dir, "g_"+strconv.Itoa(port)+".sock")
}

func (u UnixTransport) ListenControl() (net.Listener, error) {
	p := UnixControlPath(u.Dir)
	_ = os.Remove(p)
	return net.Listen("unix", p)
}

func (u UnixTransport) ListenInbox() (net.Listener, error) {
	p := UnixInboxPath(u.Dir)
	_ = os.Remove(p)
	ln, err := net.Listen("unix", p)
	if err == nil {
		_ = os.Chmod(p, 0o600)
	}
	return ln, err
}

func (u UnixTransport) DialHost(port int) (net.Conn, error) {
	return net.DialTimeout("unix", UnixGuestPath(u.Dir, port), 2*time.Second)
}

// Daemon is boss-vsockd.
type Daemon struct {
	T Transport
	// FixedLoopback binds 127.0.0.1:<vsock port> (inside a VM). False binds
	// ephemeral loopback ports (process backend: many "VMs" share one host).
	FixedLoopback bool
	// NetSetup configures the replica NIC from the bootstrap (Linux VMs only).
	NetSetup func(b *Bootstrap) error
	// NoInbox leaves the work inbox to boss-runtime (P8b images).
	NoInbox bool

	mu    sync.Mutex
	boot  *Bootstrap
	ports map[int]int
	start time.Time
	busy  int
	runs  []string
}

// Run serves until ctx ends.
func (d *Daemon) Run(ctx context.Context) error {
	d.start = time.Now()
	d.ports = map[int]int{}
	for _, port := range RelayPorts {
		addr := "127.0.0.1:0"
		if d.FixedLoopback {
			addr = "127.0.0.1:" + strconv.Itoa(port)
		}
		ln, err := net.Listen("tcp", addr)
		if err != nil {
			return fmt.Errorf("relay %d: %w", port, err)
		}
		d.ports[port] = ln.Addr().(*net.TCPAddr).Port
		go d.relay(ctx, ln, port)
	}
	if !d.NoInbox {
		inbox, err := d.T.ListenInbox()
		if err != nil {
			return fmt.Errorf("inbox listener: %w", err)
		}
		go func() { <-ctx.Done(); inbox.Close() }()
		go d.serveInbox(ctx, inbox)
	}
	ctl, err := d.T.ListenControl()
	if err != nil {
		return fmt.Errorf("control listener: %w", err)
	}
	go func() { <-ctx.Done(); ctl.Close() }()
	for {
		c, err := ctl.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			time.Sleep(50 * time.Millisecond)
			continue
		}
		go d.serveControl(c)
	}
}

func (d *Daemon) relay(ctx context.Context, ln net.Listener, port int) {
	go func() { <-ctx.Done(); ln.Close() }()
	for {
		c, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			time.Sleep(20 * time.Millisecond)
			continue
		}
		go func(c net.Conn) {
			defer c.Close()
			up, err := d.T.DialHost(port)
			if err != nil {
				// Fail closed and visibly: there is no other path out.
				_, _ = io.WriteString(c, "HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nboss-vsockd: host gateway unreachable\n")
				return
			}
			defer up.Close()
			splice(c, up)
		}(c)
	}
}

// splice copies both ways and closes write halves so HTTP framing works.
func splice(a, b net.Conn) {
	done := make(chan struct{}, 2)
	cp := func(dst, src net.Conn) {
		_, _ = io.Copy(dst, src)
		if cw, ok := dst.(interface{ CloseWrite() error }); ok {
			_ = cw.CloseWrite()
		}
		done <- struct{}{}
	}
	go cp(a, b)
	go cp(b, a)
	<-done
	<-done
}

func (d *Daemon) serveControl(c net.Conn) {
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(10 * time.Second))
	line, err := bufio.NewReader(c).ReadBytes('\n')
	if err != nil {
		return
	}
	var req Request
	resp := Response{}
	if err := json.Unmarshal(line, &req); err != nil {
		resp.Error = "bad request"
	} else {
		resp = d.handle(req)
	}
	raw, _ := json.Marshal(resp)
	_, _ = c.Write(append(raw, '\n'))
}

func (d *Daemon) handle(req Request) Response {
	d.mu.Lock()
	boot := d.boot
	d.mu.Unlock()
	base := Response{OK: true, Bootstrapped: boot != nil, Ports: d.ports,
		Uptime: time.Since(d.start).Milliseconds()}
	d.mu.Lock()
	base.BusySlots = d.busy
	if boot != nil {
		base.VMID = boot.VMID
		base.TotalSlots = slotsOf(boot)
	}
	d.mu.Unlock()
	switch req.Op {
	case "ping":
		return base
	case "bootstrap":
		if req.Bootstrap == nil || req.Bootstrap.VMID == "" {
			return Response{Error: "bootstrap: vm_id required"}
		}
		if boot != nil {
			if boot.VMID != req.Bootstrap.VMID {
				return Response{Error: "bootstrap: this VM is already " + boot.VMID}
			}
			return base // idempotent: a restarted host re-bootstraps adopted VMs
		}
		b := *req.Bootstrap
		mixSeed(b.Seed)
		if b.Mode == "replica" && d.NetSetup != nil {
			if err := d.NetSetup(&b); err != nil {
				return Response{Error: "bootstrap: net setup: " + err.Error()}
			}
		}
		b.Seed = nil // never kept
		d.mu.Lock()
		d.boot = &b
		d.mu.Unlock()
		base.Bootstrapped = true
		base.VMID = b.VMID
		return base
	case "ifaces":
		ifs, err := net.Interfaces()
		if err != nil {
			return Response{Error: err.Error()}
		}
		for _, i := range ifs {
			base.Ifaces = append(base.Ifaces, i.Name)
		}
		sort.Strings(base.Ifaces)
		return base
	case "runs":
		d.mu.Lock()
		base.Runs = append([]string(nil), d.runs...)
		d.mu.Unlock()
		return base
	case "selfcheck_egress":
		base.Selfcheck = SelfcheckEgress()
		return base
	case "selfcheck_gateway":
		if boot == nil {
			return Response{Error: "not bootstrapped"}
		}
		handle := boot.Handles["run"]
		if handle == "" {
			handle = boot.Handles["replica"]
		}
		base.Selfcheck = SelfcheckGateway(d.ports[PortRunPlane], handle)
		return base
	default:
		return Response{Error: "unknown op " + strconv.Quote(req.Op)}
	}
}

func mixSeed(seed []byte) {
	if len(seed) == 0 {
		return
	}
	// Writing mixes into the pool without crediting entropy; VMGenID does the
	// rest on restore (P8b). Best effort in the process backend.
	if f, err := os.OpenFile("/dev/urandom", os.O_WRONLY, 0); err == nil {
		_, _ = f.Write(seed)
		f.Close()
	} else {
		log.Printf("boss-vsockd: seed not mixed: %v", err)
	}
}

func slotsOf(b *Bootstrap) int {
	if b.Slots > 0 {
		return b.Slots
	}
	if b.Mode == "per_run" {
		return 1
	}
	return 4
}

// serveInbox is the P8a stand-in for boss-runtime's work inbox (HOST_API.md
// §4.5): it accepts a run when a slot is free, holds the slot for RunHoldMS,
// records the id, and answers ping/slots. A connection may carry many lines.
func (d *Daemon) serveInbox(ctx context.Context, ln net.Listener) {
	for {
		c, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			time.Sleep(20 * time.Millisecond)
			continue
		}
		go func(c net.Conn) {
			defer c.Close()
			rd := bufio.NewReader(c)
			for {
				_ = c.SetDeadline(time.Now().Add(30 * time.Second))
				line, err := rd.ReadBytes('\n')
				if err != nil {
					return
				}
				var w Work
				var reply InboxReply
				if json.Unmarshal(line, &w) != nil {
					reply.Reason = "bad_request"
				} else {
					switch w.Op {
					case "run":
						if w.RunID == "" {
							reply.Reason = "bad_request"
						} else {
							reply = d.accept(w)
						}
					case "ping", "slots":
						busy, total := d.slots()
						reply = InboxReply{OK: true, BusySlots: &busy, TotalSlots: &total}
					default:
						reply.Reason = "bad_request"
					}
				}
				raw, _ := json.Marshal(reply)
				if _, err := c.Write(append(raw, '\n')); err != nil {
					return
				}
			}
		}(c)
	}
}

func (d *Daemon) slots() (busy, total int) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.boot == nil {
		return 0, 0
	}
	return d.busy, slotsOf(d.boot)
}

func (d *Daemon) accept(w Work) InboxReply {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.boot == nil {
		return InboxReply{Reason: "draining"}
	}
	for _, r := range d.runs {
		if r == w.RunID && d.busy > 0 {
			return InboxReply{Reason: "duplicate"}
		}
	}
	if d.busy >= slotsOf(d.boot) {
		return InboxReply{Reason: "no_free_slot"}
	}
	d.busy++
	d.runs = append(d.runs, w.RunID)
	hold := time.Duration(d.boot.RunHoldMS) * time.Millisecond
	time.AfterFunc(hold, func() {
		d.mu.Lock()
		d.busy--
		d.mu.Unlock()
	})
	return InboxReply{OK: true}
}
