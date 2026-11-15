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
type Process struct {
	Root     string // per-VM directories live here
	GuestBin string // boss-vsockd binary
	// GuestArgs prefixes the guest argv (tests re-exec their own binary).
	GuestArgs []string
	GuestEnv  []string
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

func (p *Process) Create(ctx context.Context, s Spec) (*VM, error) {
	if !VMIDPattern.MatchString(s.VMID) {
		return nil, fmt.Errorf("invalid vm_id %q", s.VMID)
	}
	if err := p.Check(); err != nil {
		return nil, err
	}
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
	cmd := exec.Command(p.GuestBin, args...)
	cmd.Env = append(os.Environ(), p.GuestEnv...)
	cmd.Stdout, cmd.Stderr = console, console
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true} // survives a hostd crash
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	vm := newVM(s.VMID, cmd.Process.Pid, dir, false)
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

func (p *Process) Restore(context.Context, Spec, SnapshotRef) (*VM, error) {
	return nil, ErrNotImplemented
}
func (p *Process) Snapshot(context.Context, *VM) (SnapshotRef, error) {
	return SnapshotRef{}, ErrNotImplemented
}

func (p *Process) Stop(ctx context.Context, vm *VM) error {
	err := stopProcess(ctx, vm, 2*time.Second)
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
		watchAdopted(vm)
		out = append(out, vm)
	}
	return out, nil
}

func (p *Process) DialControl(vm *VM) (net.Conn, error) {
	return net.DialTimeout("unix", guest.UnixControlPath(vm.Dir), time.Second)
}

func (p *Process) GuestListen(vm *VM, port int) (net.Listener, error) {
	return listenUnix(guest.UnixGuestPath(vm.Dir, port))
}
