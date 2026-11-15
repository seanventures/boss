package vmm

import (
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/guest"
	"github.com/seanheiney/boss/services/sandbox-service/internal/procscan"
)

// Process runs boss-vsockd as the stand-in VM (local parity backend).
//
// Snapshots here are records only: Snapshot stores the shape's meta.json
// (after the same hygiene gate) and Restore starts a fresh guest marked
// restored. That keeps the pool, claim and GC logic above identical on every
// OS; only Firecracker has memory to save.
type Process struct {
	Root     string // per-VM directories live here
	GuestBin string // boss-vsockd binary
	// GuestArgs prefixes the guest argv (tests re-exec their own binary).
	GuestArgs []string
	GuestEnv  []string
	// SnapDir is the snapshot store (default <Root>/_snapshots).
	SnapDir string
	// RuntimeVersion is part of the snapshot key.
	RuntimeVersion string
}

func (p *Process) Name() string { return "process" }

func (p *Process) Check() error {
	if p.GuestBin == "" {
		return fmt.Errorf("process backend: guest binary not configured (BOSS_HOSTD_GUEST_BIN)")
	}
	if info, err := os.Stat(p.GuestBin); err != nil || info.IsDir() {
		return fmt.Errorf("process backend: guest binary missing at %s", p.GuestBin)
	}
	return nil
}

func (p *Process) dir(id string) string { return filepath.Join(p.Root, id) }

func (p *Process) store() Store {
	if p.SnapDir != "" {
		return Store{Dir: p.SnapDir}
	}
	return Store{Dir: filepath.Join(p.Root, "_snapshots")}
}

func (p *Process) shapeOf(s Spec) Spec {
	if s.RuntimeVersion == "" {
		s.RuntimeVersion = p.RuntimeVersion
	}
	if s.KernelDigest == "" {
		s.KernelDigest = "process"
	}
	return s
}

func (p *Process) Create(ctx context.Context, s Spec) (*VM, error) {
	return p.launch(ctx, s, nil)
}

func (p *Process) Restore(ctx context.Context, s Spec, snap SnapshotRef) (*VM, error) {
	if snap.Dir == "" {
		return nil, ErrNoSnapshot
	}
	if got := p.shapeOf(s).Shape(); got != snap.Shape {
		return nil, fmt.Errorf("snapshot %s is for another shape", snap.Key)
	}
	return p.launch(ctx, s, &snap)
}

func (p *Process) launch(ctx context.Context, s Spec, snap *SnapshotRef) (*VM, error) {
	if !VMIDPattern.MatchString(s.VMID) {
		return nil, fmt.Errorf("invalid vm_id %q", s.VMID)
	}
	if err := p.Check(); err != nil {
		return nil, err
	}
	s = p.shapeOf(s)
	dir := p.dir(s.VMID)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	console, err := os.OpenFile(filepath.Join(dir, "console.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return nil, err
	}
	defer console.Close()
	args := append(append([]string{}, p.GuestArgs...),
		"--mode=process", "--dir="+dir, "--boss-vm-id="+s.VMID)
	if snap != nil {
		args = append(args, "--restored")
	}
	cmd := exec.Command(p.GuestBin, args...)
	cmd.Env = append(os.Environ(), p.GuestEnv...)
	cmd.Stdout, cmd.Stderr = console, console
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true} // survives a hostd crash
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	vm := newVM(s.VMID, cmd.Process.Pid, dir, false)
	vm.Restored, vm.Snapshot = snap != nil, snap
	sp := s.Normalized()
	vm.Spec = &sp
	_ = writeSpec(dir, s)
	go func() { _ = cmd.Wait(); vm.markExited() }()
	// Wait for the control socket so the caller can bootstrap.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(guest.UnixControlPath(dir)); err == nil {
			return vm, nil
		}
		select {
		case <-vm.Exited:
			return nil, fmt.Errorf("guest exited during start (see %s)", filepath.Join(dir, "console.log"))
		case <-time.After(20 * time.Millisecond):
		}
	}
	return vm, nil
}

// Snapshot applies the hygiene gate and stores the shape record.
func (p *Process) Snapshot(ctx context.Context, vm *VM) (SnapshotRef, error) {
	if tainted, why := vm.Tainted(); tainted {
		return SnapshotRef{}, fmt.Errorf("%w (%s)", ErrTainted, why)
	}
	if vm.Spec == nil {
		return SnapshotRef{}, fmt.Errorf("snapshot: VM has no spec record")
	}
	shape := p.shapeOf(*vm.Spec).Shape()
	st := p.store()
	if err := os.MkdirAll(st.Dir, 0o700); err != nil {
		return SnapshotRef{}, err
	}
	dir, err := st.Begin(shape)
	if err != nil {
		return SnapshotRef{}, err
	}
	ref := SnapshotRef{Key: shape.Key(), Shape: shape, Dir: dir, CleanAttested: true, CreatedAt: time.Now().UTC(),
		SourceVMID: vm.ID, Backend: p.Name()}
	if err := st.Commit(ref); err != nil {
		_ = os.RemoveAll(dir)
		return SnapshotRef{}, err
	}
	return ref, nil
}

func (p *Process) FindSnapshot(shape Shape) (SnapshotRef, bool) {
	if shape.KernelDigest == "" {
		shape.KernelDigest = "process"
	}
	if shape.RuntimeVersion == "" {
		shape.RuntimeVersion = p.RuntimeVersion
	}
	return p.store().Find(shape)
}

func (p *Process) Snapshots() []SnapshotRef             { return p.store().List() }
func (p *Process) DeleteSnapshot(ref SnapshotRef) error { return p.store().Delete(ref) }

func (p *Process) Stop(ctx context.Context, vm *VM) error {
	err := stopProcess(ctx, vm, 500*time.Millisecond)
	if err == nil && vm.Dir != "" && strings.HasPrefix(vm.Dir, p.Root) {
		_ = os.RemoveAll(vm.Dir)
	}
	return err
}

func (p *Process) Stats(vm *VM) Stats { return procStats(vm.PID) }

func (p *Process) Scan() ([]*VM, error) {
	procs, err := procscan.Find(func(pr procscan.Proc) bool {
		return pr.Flag("boss-vm-id") != "" && strings.HasPrefix(pr.Flag("dir"), p.Root+string(os.PathSeparator))
	})
	if err != nil {
		return nil, err
	}
	var out []*VM
	for _, pr := range procs {
		vm := newVM(pr.Flag("boss-vm-id"), pr.PID, pr.Flag("dir"), true)
		vm.Spec = readSpec(vm.Dir)
		if vm.Spec != nil && vm.Spec.VMID != "" {
			vm.ID = vm.Spec.VMID // a claimed warm VM is known by its control-plane id
		}
		watchAdopted(vm)
		out = append(out, vm)
	}
	return out, nil
}

func (p *Process) DialControl(vm *VM, deadline time.Time) (net.Conn, error) {
	return dialUnix(guest.UnixControlPath(vm.Dir), deadline)
}

func (p *Process) DialInbox(vm *VM, deadline time.Time) (net.Conn, error) {
	return dialUnix(guest.UnixInboxPath(vm.Dir), deadline)
}

func dialUnix(path string, deadline time.Time) (net.Conn, error) {
	c, err := (&net.Dialer{Deadline: deadline}).Dial("unix", path)
	if err != nil {
		return nil, err
	}
	_ = c.SetDeadline(deadline)
	return c, nil
}

func (p *Process) GuestListen(vm *VM, port int) (net.Listener, error) {
	return listenUnix(guest.UnixGuestPath(vm.Dir, port))
}
