// Package vmm is boss-hostd's multi-slot VM manager (LLD §3.24: "Add a
// VMManager (Create, Restore, Snapshot, Stop, Stats, Vsock) underneath").
//
// Backends:
//   - process:     the guest relay (boss-vsockd) as a local subprocess with a
//     Unix socket per route instead of vsock. Laptop/CI parity; no isolation
//     claim. Snapshot/Restore keep the same records and hygiene gate so the
//     pool and claim logic above is exercised everywhere.
//   - firecracker: one Firecracker microVM per VM under the jailer (chroot,
//     a uid per VM, cgroup v2, netns, default seccomp; LLD §3.18), booted
//     from a shared read-only base rootfs plus a per-VM writable overlay,
//     or restored from a full snapshot of its shape (LLD §3.4).
//
// VMs are started in their own session so they outlive a boss-hostd crash;
// Scan finds them again (argv or chroot) so a restarted host adopts instead
// of duplicating.
package vmm

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/seanventures/boss/services/sandbox-service/internal/procscan"
)

// ErrNotImplemented marks seams left for a later phase.
var ErrNotImplemented = errors.New("not implemented")

// ErrTainted is the snapshot hygiene gate (LLD §3.4 S15 rule 1): a VM that
// has received a handle, an identity or a run is never snapshotted.
var ErrTainted = errors.New("snapshot refused: VM has received an identity, handle or run")

// ErrReplicaNeedsJail: a replica snapshot is only taken under the jailer,
// where every replica's guest tap has the same name (see Firecracker.Snapshot).
var ErrReplicaNeedsJail = errors.New("replica snapshots need the jailer (uniform guest tap); unjailed replicas cold boot")

// ErrNoSnapshot means no snapshot exists for the spec's shape.
var ErrNoSnapshot = errors.New("no snapshot for this shape")

// VMIDPattern is the contract for control-plane-assigned vm ids (they become
// directory names and process markers; the control plane mints "vm_<16 hex>").
var VMIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$`)

// FirecrackerID maps a vm id onto Firecracker's --id alphabet ([A-Za-z0-9-]).
func FirecrackerID(vmID string) string { return strings.ReplaceAll(vmID, "_", "-") }

// NetAttach is a replica's NIC. per_run VMs have none.
//
// Tap is the host-side link the policy is keyed on (bt<12 hex>). Without a
// jailer it is a plain tap that netpol creates before the VM starts. Under
// the jailer the VM manager owns the link (LinkOwner): it makes the VM's
// netns with GuestTap bridged to a veth whose host end is Tap, so netpol only
// applies its policy to that end, after Create.
type NetAttach struct {
	Tap     string
	MAC     string
	VMIP    string
	Gateway string
}

// GuestTap is the tap name inside a replica's netns (uniform across VMs, so
// a snapshot's network device restores in any replica's namespace).
const GuestTap = "tap0"

// LinkOwner is implemented by a backend that builds the replica's host link
// itself (jailer + netns): the host must Create the VM before applying the
// network policy to NetAttach.Tap.
type LinkOwner interface {
	OwnsReplicaLink() bool
}

// Spec is everything needed to start one VM.
type Spec struct {
	VMID              string     `json:"vm_id"`
	DeploymentID      string     `json:"deployment_id"`
	DeploymentVersion string     `json:"deployment_version"`
	Mode              string     `json:"vm_mode"` // replica | per_run
	ImageDigest       string     `json:"image_digest"`
	KernelDigest      string     `json:"kernel_digest,omitempty"`
	RuntimeVersion    string     `json:"runtime_version,omitempty"`
	VMIP              string     `json:"vm_ip,omitempty"` // replicas
	VCPU              int        `json:"vcpu"`
	MemMiB            int        `json:"mem_mib"`
	Slots             int        `json:"slots"`
	Net               *NetAttach `json:"net,omitempty"` // nil = no network device at all
	// Warm marks a pool VM: it has no control-plane identity yet, so its
	// directory is named by VMID (a host-local slot id) until it is claimed.
	Warm bool `json:"warm,omitempty"`
}

// Normalized applies the defaults every backend boots with.
func (s Spec) Normalized() Spec {
	if s.VCPU <= 0 {
		s.VCPU = 1
	}
	if s.MemMiB <= 0 {
		s.MemMiB = 256
	}
	return s
}

// Shape is the snapshot key (LLD §3.4): (image_digest, kernel_digest, vcpu,
// mem_mib, runtime_version), plus the mode, because a per_run snapshot has no
// network device and a replica snapshot has one.
type Shape struct {
	ImageDigest    string `json:"image_digest"`
	KernelDigest   string `json:"kernel_digest"`
	VCPU           int    `json:"vcpu"`
	MemMiB         int    `json:"mem_mib"`
	RuntimeVersion string `json:"runtime_version"`
	Mode           string `json:"vm_mode"`
}

// Shape is the spec's snapshot key.
func (s Spec) Shape() Shape {
	n := s.Normalized()
	return Shape{ImageDigest: n.ImageDigest, KernelDigest: n.KernelDigest, VCPU: n.VCPU, MemMiB: n.MemMiB,
		RuntimeVersion: n.RuntimeVersion, Mode: n.Mode}
}

// Key is the stable directory name of a shape's snapshot.
func (k Shape) Key() string {
	sum := sha256.Sum256([]byte(fmt.Sprintf("%s|%s|%d|%d|%s|%s", k.ImageDigest, k.KernelDigest, k.VCPU, k.MemMiB, k.RuntimeVersion, k.Mode)))
	return hex.EncodeToString(sum[:])[:24]
}

// Differs names the first field in which a running VM's recorded spec
// differs from the wanted one ("" = same). A missing record always differs.
func Differs(running *Spec, want Spec) string {
	if running == nil {
		return "no spec record (not started by this host)"
	}
	r, w := running.Normalized(), want.Normalized()
	switch {
	case r.ImageDigest != w.ImageDigest:
		return "image_digest"
	case r.Mode != w.Mode:
		return "vm_mode"
	case r.VMIP != w.VMIP:
		return "vm_ip"
	case r.VCPU != w.VCPU:
		return "vcpu"
	case r.MemMiB != w.MemMiB:
		return "mem_mib"
	case (r.Net == nil) != (w.Net == nil):
		return "network device"
	}
	return ""
}

// specFile is written 0600 into the VM directory at Create and read back by
// Scan, so an adopted VM is compared with desired state even without bbolt.
// A claimed warm VM rewrites it with its control-plane identity.
const specFile = "boss-spec.json"

func writeSpec(dir string, s Spec) error {
	raw, err := json.Marshal(s.Normalized())
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, specFile), raw, 0o600)
}

func readSpec(dir string) *Spec {
	raw, err := os.ReadFile(filepath.Join(dir, specFile))
	if err != nil {
		return nil
	}
	var s Spec
	if json.Unmarshal(raw, &s) != nil {
		return nil
	}
	return &s
}

// VM is a running VM handle.
type VM struct {
	ID      string // the slot/dir id (a warm VM keeps it after the claim)
	PID     int
	Dir     string
	Spec    *Spec         // what it was started with (from boss-spec.json)
	Adopted bool          // found by Scan, not started by this process
	Exited  chan struct{} // closed when the VM process is gone
	// Restored is true when the VM came from a snapshot.
	Restored bool
	// UID is the jailer uid (0 = not jailed).
	UID int
	// Jailed reports the Firecracker process runs under the jailer chroot.
	Jailed bool
	// Snapshot is the snapshot this VM was restored from (readers are revoked at Stop).
	Snapshot *SnapshotRef
	// Meta is the root-owned directory holding boss-hostd's records for the
	// VM (boss-spec.json, console.log); Dir when there is no jail.
	Meta string

	once   sync.Once
	mu     sync.Mutex
	taint  string
	revoke func() // revokes the uid's read grants (jailed)
}

func (v *VM) markExited() { v.once.Do(func() { close(v.Exited) }) }

// Taint records that the VM received an identity, a handle or a run. From
// then on Snapshot refuses it (S15).
func (v *VM) Taint(reason string) {
	v.mu.Lock()
	if v.taint == "" {
		v.taint = reason
	}
	v.mu.Unlock()
}

// Tainted reports whether (and why) the VM may no longer be snapshotted.
func (v *VM) Tainted() (bool, string) {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.taint != "", v.taint
}

// Claim records the control-plane identity a warm VM was given: the spec
// file now names the real vm_id and the VM is no longer warm.
func (v *VM) Claim(s Spec) {
	s.Warm = false
	n := s.Normalized()
	v.mu.Lock()
	v.Spec = &n
	v.mu.Unlock()
	if dir := v.metaDir(); dir != "" {
		_ = writeSpec(dir, s)
	}
}

func (v *VM) metaDir() string {
	if v.Meta != "" {
		return v.Meta
	}
	return v.Dir
}

// SnapshotRef names one snapshot in the store (meta.json).
type SnapshotRef struct {
	Key   string `json:"key"`
	Shape Shape  `json:"shape"`
	Dir   string `json:"dir"`
	// CleanAttested: the source VM had received no identity, handle or run.
	CleanAttested bool      `json:"clean_attested"`
	CreatedAt     time.Time `json:"created_at"`
	SourceVMID    string    `json:"source_vm_id"`
	MemBytes      int64     `json:"mem_bytes"`
	Backend       string    `json:"backend"`
}

// Stats is a cheap per-VM sample.
type Stats struct {
	RSSKiB int64
}

// Manager is the backend-neutral VM lifecycle.
type Manager interface {
	Name() string
	Check() error
	Create(ctx context.Context, s Spec) (*VM, error)
	// Restore starts a VM from a snapshot of s.Shape().
	Restore(ctx context.Context, s Spec, snap SnapshotRef) (*VM, error)
	// Snapshot takes a full snapshot of a clean VM (ErrTainted otherwise) and
	// stores it under the VM's shape. The VM keeps running.
	Snapshot(ctx context.Context, vm *VM) (SnapshotRef, error)
	// FindSnapshot returns the stored snapshot for a shape.
	FindSnapshot(shape Shape) (SnapshotRef, bool)
	Snapshots() []SnapshotRef
	DeleteSnapshot(ref SnapshotRef) error
	Stop(ctx context.Context, vm *VM) error
	Stats(vm *VM) Stats
	Scan() ([]*VM, error)
	// DialControl opens the host-initiated control channel (vsock port 5000),
	// giving up at deadline.
	DialControl(vm *VM, deadline time.Time) (net.Conn, error)
	// DialInbox opens the VM's work inbox (vsock port 7105).
	DialInbox(vm *VM, deadline time.Time) (net.Conn, error)
	// GuestListen is the host end of guest-initiated connections on port.
	// The listener identifies the VM: nothing else may connect to it.
	GuestListen(vm *VM, port int) (net.Listener, error)
}

// Attestor is implemented by backends that measure the heartbeat facts
// jailer_on and seccomp_default from the running processes.
type Attestor interface {
	// JailerOn is (true, "") when the jailer is configured, usable and every
	// running VM is jailed; otherwise (false, reason).
	JailerOn() (bool, string)
	// SeccompOn is (true, "") when every running VM is under a seccomp filter
	// (Seccomp: 2 in /proc/<pid>/status); otherwise (false, reason).
	SeccompOn() (bool, string)
}

func newVM(id string, pid int, dir string, adopted bool) *VM {
	return &VM{ID: id, PID: pid, Dir: dir, Meta: dir, Adopted: adopted, Exited: make(chan struct{})}
}

// watchAdopted polls a process we did not start (no wait() possible).
func watchAdopted(vm *VM) {
	go func() {
		t := time.NewTicker(200 * time.Millisecond)
		defer t.Stop()
		for range t.C {
			if !procscan.Alive(vm.PID) {
				vm.markExited()
				return
			}
		}
	}()
}

// stopProcess sends SIGTERM, waits up to grace, then SIGKILL.
func stopProcess(ctx context.Context, vm *VM, grace time.Duration) error {
	if vm.PID <= 0 {
		return nil
	}
	_ = syscall.Kill(vm.PID, syscall.SIGTERM)
	select {
	case <-vm.Exited:
		return nil
	case <-time.After(grace):
	case <-ctx.Done():
	}
	_ = syscall.Kill(vm.PID, syscall.SIGKILL)
	select {
	case <-vm.Exited:
		return nil
	case <-time.After(3 * time.Second):
		if procscan.Alive(vm.PID) {
			return fmt.Errorf("vm %s (pid %d) did not exit", vm.ID, vm.PID)
		}
		vm.markExited()
		return nil
	}
}

func listenUnix(path string) (net.Listener, error) {
	_ = os.Remove(path)
	ln, err := net.Listen("unix", path)
	if err != nil {
		return nil, err
	}
	_ = os.Chmod(path, 0o600)
	return ln, nil
}

func procStats(pid int) Stats {
	raw, err := os.ReadFile(fmt.Sprintf("/proc/%d/statm", pid))
	if err != nil {
		return Stats{}
	}
	var size, rss int64
	_, _ = fmt.Sscanf(string(raw), "%d %d", &size, &rss)
	return Stats{RSSKiB: rss * int64(os.Getpagesize()) / 1024}
}
