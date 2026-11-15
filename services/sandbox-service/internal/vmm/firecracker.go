package vmm

import (
	"context"
	"encoding/json"
	"fmt"
	"hash/fnv"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/guest"
	"github.com/seanheiney/boss/services/sandbox-service/internal/procscan"
)

// Firecracker cold-boots one microVM per VM from a shared read-only base
// rootfs whose init is boss-vsockd (LLD §3.4: no per-VM disk copy).
//
// P8b seams: Launcher (the jailer wraps the same argv), Restore/Snapshot (the
// API socket is already on, which snapshots need), and the overlay drive.
type Firecracker struct {
	Root   string // per-VM dirs (keep short: Unix socket paths are ≤ 107 bytes)
	Bin    string
	Kernel string
	Rootfs string // read-only base, shared by every VM
	// Init is the guest init path inside the rootfs.
	Init string
	// Launcher, if set, prefixes the argv (P8b: the jailer).
	Launcher []string
}

func (f *Firecracker) Name() string { return "firecracker" }

// Check keeps the named reasons of backend.Firecracker.Check.
func (f *Firecracker) Check() error {
	if runtime.GOOS != "linux" {
		return fmt.Errorf("firecracker backend: requires Linux (running on %s)", runtime.GOOS)
	}
	if fd, err := os.OpenFile("/dev/kvm", os.O_RDWR, 0); err != nil {
		return fmt.Errorf("firecracker backend: /dev/kvm not usable (%v) — KVM missing or user lacks kvm group", err)
	} else {
		fd.Close()
	}
	if f.Bin == "" {
		return fmt.Errorf("firecracker backend: firecracker binary not configured (BOSS_SANDBOX_FC_BIN)")
	}
	if info, err := os.Stat(f.Bin); err != nil || info.IsDir() {
		return fmt.Errorf("firecracker backend: firecracker binary missing at %s", f.Bin)
	}
	if f.Kernel == "" {
		return fmt.Errorf("firecracker backend: kernel not configured (BOSS_SANDBOX_FC_KERNEL)")
	}
	if _, err := os.Stat(f.Kernel); err != nil {
		return fmt.Errorf("firecracker backend: kernel image missing at %s (run scripts/sandbox/microvm-setup.sh)", f.Kernel)
	}
	if f.Rootfs == "" {
		return fmt.Errorf("firecracker backend: rootfs not configured (BOSS_SANDBOX_FC_ROOTFS)")
	}
	if _, err := os.Stat(f.Rootfs); err != nil {
		return fmt.Errorf("firecracker backend: rootfs image missing at %s (run scripts/sandbox/hostd-guest-bake.sh)", f.Rootfs)
	}
	return nil
}

func (f *Firecracker) init() string {
	if f.Init != "" {
		return f.Init
	}
	return "/sbin/boss-vsockd"
}

func (f *Firecracker) dir(id string) string { return filepath.Join(f.Root, id) }

// guestCID is a stable per-VM CID ≥ 3. Firecracker's vsock is per-VM over a
// Unix socket, so CIDs never collide on the host; a distinct one per VM keeps
// logs unambiguous.
func guestCID(id string) uint32 {
	h := fnv.New32a()
	_, _ = h.Write([]byte(id))
	return 3 + h.Sum32()%0x7ffffff0
}

// VMConfig builds the Firecracker config. Exported for tests: a per_run VM
// must have no network-interfaces entry at all.
func (f *Firecracker) VMConfig(s Spec, dir string) map[string]any {
	vcpu, mem := s.VCPU, s.MemMiB
	if vcpu <= 0 {
		vcpu = 1
	}
	if mem <= 0 {
		mem = 256
	}
	boot := "console=ttyS0 reboot=k panic=1 pci=off quiet ipv6.disable=1 init=" + f.init()
	cfg := map[string]any{
		"boot-source": map[string]any{"kernel_image_path": f.Kernel, "boot_args": boot},
		"drives": []map[string]any{
			{"drive_id": "rootfs", "path_on_host": f.Rootfs, "is_root_device": true, "is_read_only": true},
		},
		"machine-config": map[string]any{"vcpu_count": vcpu, "mem_size_mib": mem},
		"vsock":          map[string]any{"guest_cid": guestCID(s.VMID), "uds_path": filepath.Join(dir, "v.sock")},
	}
	if s.Net != nil {
		cfg["network-interfaces"] = []map[string]any{{
			"iface_id": "eth0", "guest_mac": s.Net.MAC, "host_dev_name": s.Net.Tap,
		}}
	}
	return cfg
}

func (f *Firecracker) Create(ctx context.Context, s Spec) (*VM, error) {
	if !VMIDPattern.MatchString(s.VMID) {
		return nil, fmt.Errorf("invalid vm_id %q", s.VMID)
	}
	if err := f.Check(); err != nil {
		return nil, err
	}
	dir := f.dir(s.VMID)
	_ = os.RemoveAll(dir) // stale sockets from a dead VM of the same id
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	raw, err := json.MarshalIndent(f.VMConfig(s, dir), "", "  ")
	if err != nil {
		return nil, err
	}
	cfgPath := filepath.Join(dir, "vm.json")
	if err := os.WriteFile(cfgPath, raw, 0o600); err != nil {
		return nil, err
	}
	console, err := os.OpenFile(filepath.Join(dir, "console.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return nil, err
	}
	defer console.Close()
	argv := append(append([]string{}, f.Launcher...), f.Bin,
		"--api-sock", filepath.Join(dir, "fc.sock"), "--config-file", cfgPath, "--id", FirecrackerID(s.VMID))
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Stdout, cmd.Stderr = console, console
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	vm := newVM(s.VMID, cmd.Process.Pid, dir, false)
	go func() { _ = cmd.Wait(); vm.markExited() }()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(filepath.Join(dir, "v.sock")); err == nil {
			return vm, nil
		}
		select {
		case <-vm.Exited:
			return nil, fmt.Errorf("firecracker exited during start: %s", tail(filepath.Join(dir, "console.log")))
		case <-time.After(20 * time.Millisecond):
		}
	}
	return vm, nil
}

func tail(path string) string {
	raw, _ := os.ReadFile(path)
	if len(raw) > 600 {
		raw = raw[len(raw)-600:]
	}
	return strings.TrimSpace(string(raw))
}

func (f *Firecracker) Restore(context.Context, Spec, SnapshotRef) (*VM, error) {
	return nil, ErrNotImplemented
}
func (f *Firecracker) Snapshot(context.Context, *VM) (SnapshotRef, error) {
	return SnapshotRef{}, ErrNotImplemented
}

func (f *Firecracker) Stop(ctx context.Context, vm *VM) error {
	// Replicas are drained before Stop (P8b); P8a stops are immediate.
	err := stopProcess(ctx, vm, 500*time.Millisecond)
	if err == nil && vm.Dir != "" && strings.HasPrefix(vm.Dir, f.Root) {
		_ = os.RemoveAll(vm.Dir)
	}
	return err
}

func (f *Firecracker) Stats(vm *VM) Stats { return procStats(vm.PID) }

// Scan finds firecracker processes whose API socket lives under Root.
func (f *Firecracker) Scan() ([]*VM, error) {
	procs, err := procscan.Find(func(pr procscan.Proc) bool {
		if len(pr.Args) == 0 {
			return false
		}
		joined := strings.Join(pr.Args, " ")
		return strings.Contains(joined, "firecracker") &&
			strings.HasPrefix(pr.Flag("api-sock"), f.Root+string(os.PathSeparator)) && pr.Flag("id") != ""
	})
	if err != nil {
		return nil, err
	}
	var out []*VM
	for _, pr := range procs {
		dir := filepath.Dir(pr.Flag("api-sock"))
		vm := newVM(filepath.Base(dir), pr.PID, dir, true) // the dir name is the vm id
		watchAdopted(vm)
		out = append(out, vm)
	}
	return out, nil
}

// DialControl uses Firecracker's host-initiated vsock handshake:
// "CONNECT <port>\n" → "OK <host port>\n" on the VM's Unix socket.
func (f *Firecracker) DialControl(vm *VM) (net.Conn, error) {
	c, err := net.DialTimeout("unix", filepath.Join(vm.Dir, "v.sock"), time.Second)
	if err != nil {
		return nil, err
	}
	_ = c.SetDeadline(time.Now().Add(time.Second))
	if _, err := c.Write([]byte("CONNECT " + strconv.Itoa(guest.PortControl) + "\n")); err != nil {
		c.Close()
		return nil, err
	}
	var line []byte
	buf := make([]byte, 1)
	for len(line) < 64 {
		if _, err := c.Read(buf); err != nil {
			c.Close()
			return nil, fmt.Errorf("vsock handshake: %w", err)
		}
		if buf[0] == '\n' {
			break
		}
		line = append(line, buf[0])
	}
	if !strings.HasPrefix(string(line), "OK ") {
		c.Close()
		return nil, fmt.Errorf("vsock handshake refused: %q", string(line))
	}
	_ = c.SetDeadline(time.Time{})
	return c, nil
}

// GuestListen listens on <uds>_<port>, where Firecracker forwards
// guest-initiated vsock connections to that port.
func (f *Firecracker) GuestListen(vm *VM, port int) (net.Listener, error) {
	return listenUnix(filepath.Join(vm.Dir, "v.sock_"+strconv.Itoa(port)))
}
