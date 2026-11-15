// Package guest is the in-guest side of the host channel: boss-vsockd.
//
// It runs inside every VM (and, for the process backend, as the stand-in "VM"
// process) and does three things, holding no credential of its own:
//   - control server on vsock port 5000: bootstrap, ping, ifaces, selfcheck;
//   - relay: 127.0.0.1:71xx → the same vsock port on the host (the guest
//     gateway in boss-hostd), so tools only ever see loopback base URLs;
//   - as PID 1 in a Firecracker VM: minimal init (mounts, the writable overlay
//     root, lo up, and for replicas the routed /32 on eth0 from the bootstrap).
//
// Snapshot hygiene on the guest side (LLD §3.4 S15): the bootstrap after a
// restore reseeds the kernel RNG with the host's 32-byte seed (RNDADDENTROPY
// plus RNDRESEEDCRNG, so clones diverge at once), re-reads the clock, and
// sets the replica's MAC and IP. boss-vsockd holds no HTTP client, key or
// credential, so there is nothing else to rebuild.
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
	PortConnect  = 7105 // guest → host (CONNECT relay)
	PortInbox    = 7105 // host → guest (work inbox; a separate vsock direction)
	HostCID      = 2
)

// RelayPorts are the guest-initiated routes, in table order.
var RelayPorts = []int{PortModel, PortGit, PortMCP, PortRunPlane, PortConnect}

// Request is one control-channel request (one JSON line).
type Request struct {
	Op        string     `json:"op"` // bootstrap | ping | ifaces | rng | runs | selfcheck_egress | selfcheck_gateway
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
	// CID is the VM's vsock context id as the host knows it (informational:
	// the host identifies a VM by its socket, never by CID).
	CID uint32 `json:"cid,omitempty"`
	// HostTimeUnixNano is the host clock at bootstrap; a restored guest sets
	// its clock from it.
	HostTimeUnixNano int64 `json:"host_time_unix_nano,omitempty"`
	// Restored tells the guest it came from a snapshot (it reseeds either way).
	Restored bool `json:"restored,omitempty"`
	// Slots is how many runs this VM takes at once (replica default 4).
	Slots int `json:"slots,omitempty"`
	// RunHoldMS: the P8a stand-in inbox holds an accepted run this long
	// before freeing its slot (boss-runtime replaces the stand-in).
	RunHoldMS int `json:"run_hold_ms,omitempty"`
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
	BusySlots    int               `json:"busy_slots"`
	TotalSlots   int               `json:"total_slots"`
	Runs         []string          `json:"runs,omitempty"` // op "runs": run ids the inbox accepted
	// RNG is 32 hex chars from the guest's RNG after bootstrap (op "rng";
	// P8b-T4: restored clones must differ).
	RNG string `json:"rng,omitempty"`
	// Reseeded reports the kernel RNG took the bootstrap seed (and was
	// re-keyed at once); Restored echoes the bootstrap.
	Reseeded bool `json:"reseeded,omitempty"`
	Restored bool `json:"restored,omitempty"`
	// ClockUnix is the guest's wall clock at the answer (seconds).
	ClockUnix int64 `json:"clock_unix,omitempty"`
}

// Work is one line to a VM's work inbox (HOST_API.md §4.5): {"op":"run",…}
// hands a run over (never a run token); {"op":"ping"|"slots"} asks for slots.
// One answer line per request; a connection may carry many.
type Work struct {
	Op        string `json:"op"` // run | ping | slots
	RunID     string `json:"run_id,omitempty"`
	JobID     string `json:"job_id,omitempty"`
	ReplicaID string `json:"replica_id,omitempty"`
	Epoch     int64  `json:"epoch,omitempty"`
	// Handles are the run's placeholders ("mdl", "mcpgw", …, worthless off
	// this VM) and "mcp_servers" ({name: loopback URL}); never a token (P8b).
	Handles map[string]any `json:"handles,omitempty"`
}

// InboxReply answers a Work line. Refusal reasons: no_free_slot, draining,
// duplicate, bad_request.
type InboxReply struct {
	OK         bool   `json:"ok"`
	Reason     string `json:"reason,omitempty"`
	BusySlots  *int   `json:"busy_slots,omitempty"`
	TotalSlots *int   `json:"total_slots,omitempty"`
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
