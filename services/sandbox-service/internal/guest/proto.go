// Package guest is the in-guest side of the host channel: boss-vsockd.
//
// It runs inside every VM (and, for the process backend, as the stand-in "VM"
// process) and does three things, holding no credential of its own:
//   - control server on vsock port 5000: bootstrap, ping, ifaces, selfcheck;
//   - relay: 127.0.0.1:71xx → the same vsock port on the host (the guest
//     gateway in boss-hostd), so tools only ever see loopback base URLs;
//   - as PID 1 in a Firecracker VM: minimal init (mounts, lo up, and for
//     replicas the routed /32 on eth0 from the bootstrap).
//
// The process backend swaps vsock for Unix sockets in the VM directory, so CI
// exercises the same relay and the same host gateway code (LLD §3.8, S6).
package guest

import (
	"bufio"
	"encoding/json"
	"io"
	"time"
)

// Well-known vsock ports (LLD §3.8).
const (
	PortControl  = 5000
	PortModel    = 7101
	PortGit      = 7102
	PortMCP      = 7103
	PortRunPlane = 7104
	PortConnect  = 7105
	HostCID      = 2
)

// RelayPorts are the guest-initiated routes, in table order.
var RelayPorts = []int{PortModel, PortGit, PortMCP, PortRunPlane, PortConnect}

// Request is one control-channel request (one JSON line).
type Request struct {
	Op        string     `json:"op"` // bootstrap | ping | ifaces | selfcheck_egress | selfcheck_gateway
	Bootstrap *Bootstrap `json:"bootstrap,omitempty"`
}

// Bootstrap is the per-VM identity the host hands over at start (LLD §3.3).
// It carries no token: handles are opaque references only this VM's socket
// can redeem.
type Bootstrap struct {
	VMID    string `json:"vm_id"`
	Mode    string `json:"vm_mode"`
	VMIP    string `json:"vm_ip,omitempty"`   // replica only
	Gateway string `json:"gateway,omitempty"` // replica only: 169.254.1.1 (proxy-ARP)
	VIP     string `json:"vip,omitempty"`
	MAC     string `json:"mac,omitempty"`
	Seed    []byte `json:"seed"` // 32 random bytes, mixed into the guest RNG
	// Handles are opaque: "replica" for a replica's run-plane calls; for a
	// per_run claim also "model", "egress", "mcp" and "run".
	Handles map[string]string `json:"handles,omitempty"`
}

// Response is one control-channel reply.
type Response struct {
	OK           bool              `json:"ok"`
	Error        string            `json:"error,omitempty"`
	VMID         string            `json:"vm_id,omitempty"`
	Bootstrapped bool              `json:"bootstrapped"`
	Ports        map[int]int       `json:"ports,omitempty"` // vsock port → bound loopback port
	Ifaces       []string          `json:"ifaces,omitempty"`
	Selfcheck    map[string]string `json:"selfcheck,omitempty"`
	Uptime       int64             `json:"uptime_ms,omitempty"`
}

// Exchange writes one request and reads one reply on an open stream.
func Exchange(rw io.ReadWriter, req Request, deadline time.Time) (*Response, error) {
	if d, ok := rw.(interface{ SetDeadline(time.Time) error }); ok {
		_ = d.SetDeadline(deadline)
	}
	raw, err := json.Marshal(req)
	if err != nil {
		return nil, err
	}
	if _, err := rw.Write(append(raw, '\n')); err != nil {
		return nil, err
	}
	line, err := bufio.NewReader(rw).ReadBytes('\n')
	if err != nil {
		return nil, err
	}
	var resp Response
	if err := json.Unmarshal(line, &resp); err != nil {
		return nil, err
	}
	return &resp, nil
}
