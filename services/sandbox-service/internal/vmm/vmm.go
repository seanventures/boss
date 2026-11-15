// Package vmm is boss-hostd's multi-slot VM manager (LLD §3.24: "Add a
// VMManager (Create, Restore, Snapshot, Stop, Stats, Vsock) underneath").
//
// Backends:
//   - process:     the guest relay (boss-vsockd) as a local subprocess with a
//     Unix socket per route instead of vsock. Laptop/CI parity; no isolation
//     claim.
//   - firecracker: one Firecracker microVM per VM, cold boot from a shared
//     read-only base rootfs. Snapshots/restore and the jailer are P8b; their
//     seams are Restore/Snapshot below and the Launcher field.
//
// VMs are started in their own session so they outlive a boss-hostd crash;
// Scan finds them again by argv so a restarted host adopts instead of
// duplicating.
package vmm

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/procscan"
)

// ErrNotImplemented marks the P8b seams.
var ErrNotImplemented = errors.New("not implemented until P8b")

// VMIDPattern is the contract for control-plane-assigned vm ids (they become
// directory names and process markers; the control plane mints "vm_<16 hex>").
var VMIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$`)

// FirecrackerID maps a vm id onto Firecracker's --id alphabet ([A-Za-z0-9-]).
func FirecrackerID(vmID string) string { return strings.ReplaceAll(vmID, "_", "-") }

// NetAttach is a replica's NIC. per_run VMs have none.
type NetAttach struct {
	Tap     string
	MAC     string
	VMIP    string
	Gateway string
}

// Spec is everything needed to start one VM.
type Spec struct {
	VMID              string
	DeploymentID      string
	DeploymentVersion string
	Mode              string // replica | per_run
	ImageDigest       string
	VCPU              int
	MemMiB            int
	Net               *NetAttach // nil = no network device at all
}

// VM is a running VM handle.
type VM struct {
	ID      string
	PID     int
	Dir     string
	Adopted bool          // found by Scan, not started by this process
	Exited  chan struct{} // closed when the VM process is gone
	once    sync.Once
}

func (v *VM) markExited() { v.once.Do(func() { close(v.Exited) }) }

// SnapshotRef names a snapshot (P8b).
type SnapshotRef struct{ Key string }

// Stats is a cheap per-VM sample.
type Stats struct {
	RSSKiB int64
}

// Manager is the backend-neutral VM lifecycle.
type Manager interface {
	Name() string
	Check() error
	Create(ctx context.Context, s Spec) (*VM, error)
	Restore(ctx context.Context, s Spec, snap SnapshotRef) (*VM, error) // P8b
	Snapshot(ctx context.Context, vm *VM) (SnapshotRef, error)          // P8b
	Stop(ctx context.Context, vm *VM) error
	Stats(vm *VM) Stats
	Scan() ([]*VM, error)
	// DialControl opens the host-initiated control channel (vsock port 5000).
	DialControl(vm *VM) (net.Conn, error)
	// GuestListen is the host end of guest-initiated connections on port.
	// The listener identifies the VM: nothing else may connect to it.
	GuestListen(vm *VM, port int) (net.Listener, error)
}

func newVM(id string, pid int, dir string, adopted bool) *VM {
	return &VM{ID: id, PID: pid, Dir: dir, Adopted: adopted, Exited: make(chan struct{})}
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
