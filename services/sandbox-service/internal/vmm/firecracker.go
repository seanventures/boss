package vmm

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"hash/fnv"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/seanventures/boss/services/sandbox-service/internal/guest"
	"github.com/seanventures/boss/services/sandbox-service/internal/procscan"
)

// Firecracker runs one microVM per VM.
//
// Disks (LLD §3.4): the shared read-only base rootfs (hard-linked into every
// jail) plus a per-VM writable overlay drive (sparse, from a host template).
// Snapshots: a full snapshot per shape, restored with the memory file mapped
// MAP_PRIVATE. Jailer: see jail.go. Without a Jailer the backend behaves as in
// P8a (no isolation claim; jailer_on=false).
type Firecracker struct {
	Root   string // unjailed per-VM dirs (keep short: Unix socket paths are ≤ 107 bytes)
	Bin    string
	Kernel string
	Rootfs string // read-only base, shared by every VM
	// Init is the guest init path inside the rootfs.
	Init string
	// Launcher, if set and Jailer is nil, prefixes the argv (tests).
	Launcher []string
	// Jailer, when set, wraps every VM (LLD §3.18). nil = P8a behaviour.
	Jailer *Jailer
	// SnapDir is the snapshot store (default <Root>/_snapshots).
	SnapDir string
	// OverlayMiB is the per-VM writable overlay drive size (0 = no overlay).
	OverlayMiB int
	// RuntimeVersion is part of the snapshot key (the baked boss-runtime).
	RuntimeVersion string
	// Mke2fs is the ext4 formatter for the overlay template (default mke2fs).
	Mke2fs string

	mu           sync.Mutex
	kernelDigest string
}

func (f *Firecracker) Name() string { return "firecracker" }

// Check keeps the named reasons of backend.Firecracker.Check and adds the
// jailer's (jail.go).
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
	if f.OverlayMiB > 0 {
		if _, err := exec.LookPath(f.mke2fs()); err != nil {
			return fmt.Errorf("firecracker backend: overlay_tool_missing: %s not found (install e2fsprogs)", f.mke2fs())
		}
	}
	if f.Jailer != nil {
		if err := f.Jailer.Check(); err != nil {
			return fmt.Errorf("firecracker backend: %w", err)
		}
	}
	return nil
}

// SeccompOn implements Attestor: true when every running Firecracker VM of
// this host reports "Seccomp: 2" (filter mode) in /proc/<pid>/status.
// boss-hostd never passes --no-seccomp or a custom filter; this measures it.
func (f *Firecracker) SeccompOn() (bool, string) {
	vms, err := f.Scan()
	if err != nil {
		return false, "process scan failed: " + err.Error()
	}
	for _, vm := range vms {
		if mode := procscan.SeccompMode(vm.PID); mode != 2 {
			return false, fmt.Sprintf("VM %s runs with seccomp mode %d", vm.ID, mode)
		}
	}
	return true, ""
}

// JailerOn implements Attestor: true only when the jailer is configured and
// usable and every running Firecracker VM of this host runs inside it.
func (f *Firecracker) JailerOn() (bool, string) {
	if f.Jailer == nil {
		return false, "jailer not configured (BOSS_HOSTD_JAILER_BIN)"
	}
	if err := f.Jailer.Check(); err != nil {
		return false, err.Error()
	}
	vms, err := f.Scan()
	if err != nil {
		return false, "process scan failed: " + err.Error()
	}
	for _, vm := range vms {
		if !vm.Jailed {
			return false, "VM " + vm.ID + " runs outside the jailer"
		}
	}
	return true, ""
}

func (f *Firecracker) init() string {
	if f.Init != "" {
		return f.Init
	}
	return "/sbin/boss-vsockd"
}

func (f *Firecracker) mke2fs() string {
	if f.Mke2fs != "" {
		return f.Mke2fs
	}
	return "mke2fs"
}

func (f *Firecracker) store() Store {
	if f.SnapDir != "" {
		return Store{Dir: f.SnapDir}
	}
	return Store{Dir: filepath.Join(f.Root, "_snapshots")}
}

// KernelDigest is sha256 of the kernel image (computed once).
func (f *Firecracker) KernelDigest() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.kernelDigest != "" || f.Kernel == "" {
		return f.kernelDigest
	}
	fh, err := os.Open(f.Kernel)
	if err != nil {
		return ""
	}
	defer fh.Close()
	h := sha256.New()
	if _, err := io.Copy(h, fh); err != nil {
		return ""
	}
	f.kernelDigest = "sha256:" + hex.EncodeToString(h.Sum(nil))
	return f.kernelDigest
}

// shapeOf fills the host-known parts of the snapshot key.
func (f *Firecracker) shapeOf(s Spec) Spec {
	if s.KernelDigest == "" {
		s.KernelDigest = f.KernelDigest()
	}
	if s.RuntimeVersion == "" {
		s.RuntimeVersion = f.RuntimeVersion
	}
	return s
}

// ---- layout ---------------------------------------------------------------

// layout is where one VM's files live and how Firecracker names them.
type layout struct {
	host   string // directory holding the sockets and disks (the chroot root when jailed)
	meta   string // root-owned directory for boss-hostd's own records
	jailed bool
}

// guest is the path Firecracker uses for a file in the VM directory.
func (l layout) guest(name string) string {
	if l.jailed {
		return "/" + name
	}
	return filepath.Join(l.host, name)
}

func (l layout) file(name string) string { return filepath.Join(l.host, name) }

func (l layout) apiSock() string {
	if l.jailed {
		return filepath.Join(l.host, "run", "firecracker.socket")
	}
	return filepath.Join(l.host, "fc.sock")
}

func (f *Firecracker) layoutFor(vmID string) layout {
	if f.Jailer != nil {
		fcid := FirecrackerID(vmID)
		return layout{host: f.Jailer.chroot(fcid), meta: f.Jailer.jailDir(fcid), jailed: true}
	}
	dir := filepath.Join(f.Root, vmID)
	return layout{host: dir, meta: dir}
}

// guestCID is a stable per-VM CID ≥ 3. Firecracker's vsock is per-VM over a
// Unix socket, so CIDs never collide on the host; a distinct one per VM keeps
// logs unambiguous. A restored clone keeps its snapshot's CID (Firecracker
// fixes it at load); the host identifies VMs by socket, never by CID.
func guestCID(id string) uint32 {
	h := fnv.New32a()
	_, _ = h.Write([]byte(id))
	return 3 + h.Sum32()%0x7ffffff0
}

// GuestCID is the vsock CID a VM id maps to (informational in the bootstrap).
func GuestCID(id string) uint32 { return guestCID(id) }

// Rate limiters (LLD §3.6): disk 50 MB/s and 1000 IOPS; replica tap
// 100 Mbit/s each way. Token buckets refill once per second.
const (
	DiskBytesPerSec = 50_000_000
	DiskIOPS        = 1000
	NetBytesPerSec  = 12_500_000 // 100 Mbit/s
)

func diskLimiter() map[string]any {
	return map[string]any{
		"bandwidth": map[string]any{"size": DiskBytesPerSec, "refill_time": 1000},
		"ops":       map[string]any{"size": DiskIOPS, "refill_time": 1000},
	}
}

func netLimiter() map[string]any {
	return map[string]any{"bandwidth": map[string]any{"size": NetBytesPerSec, "refill_time": 1000}}
}

// VMConfig builds the Firecracker config for a cold boot. Exported for tests:
// a per_run VM must have no network-interfaces entry at all.
func (f *Firecracker) VMConfig(s Spec, dir string) map[string]any {
	return f.config(s, layout{host: dir, meta: dir})
}

func (f *Firecracker) config(s Spec, l layout) map[string]any {
	s = s.Normalized()
	boot := "console=ttyS0 reboot=k panic=1 pci=off quiet ipv6.disable=1 init=" + f.init()
	kernel, rootfs := f.Kernel, f.Rootfs
	if l.jailed {
		kernel, rootfs = l.guest("vmlinux"), l.guest("rootfs.ext4")
	}
	drives := []map[string]any{
		{"drive_id": "rootfs", "path_on_host": rootfs, "is_root_device": true, "is_read_only": true, "rate_limiter": diskLimiter()},
	}
	if f.OverlayMiB > 0 {
		drives = append(drives, map[string]any{"drive_id": "overlay", "path_on_host": l.guest("overlay.ext4"),
			"is_root_device": false, "is_read_only": false, "rate_limiter": diskLimiter()})
	}
	cfg := map[string]any{
		"boot-source":    map[string]any{"kernel_image_path": kernel, "boot_args": boot},
		"drives":         drives,
		"machine-config": map[string]any{"vcpu_count": s.VCPU, "mem_size_mib": s.MemMiB},
		"vsock":          map[string]any{"guest_cid": guestCID(s.VMID), "uds_path": l.guest("v.sock")},
	}
	if s.Net != nil {
		dev := s.Net.Tap
		if l.jailed {
			dev = GuestTap // uniform inside the VM's netns, so a snapshot restores anywhere
		}
		cfg["network-interfaces"] = []map[string]any{{
			"iface_id": "eth0", "guest_mac": s.Net.MAC, "host_dev_name": dev,
			"rx_rate_limiter": netLimiter(), "tx_rate_limiter": netLimiter(),
		}}
		// No MMDS: the bootstrap over vsock carries the same data, nothing
		// consumes it, and the in-guest "meta skuid != 0 drop" rule does not
		// exist yet (review of PR #112). Replicas therefore reach
		// 169.254.169.254 only through the tap, where the host drops and
		// counts it (meta_).
	}
	return cfg
}

// ---- jail preparation -----------------------------------------------------

// prepare lays out a VM directory: kernel and base rootfs linked in, a fresh
// (or snapshot-copied) overlay, and ownership for the jailer uid.
func (f *Firecracker) prepare(s Spec, l layout, uid int, snap *SnapshotRef) (cleanup func(), err error) {
	_ = os.RemoveAll(l.meta) // stale files from a dead VM of the same id
	if l.meta != l.host {
		if err := os.MkdirAll(l.meta, 0o700); err != nil {
			return nil, err
		}
	}
	if err := os.MkdirAll(l.host, 0o700); err != nil {
		return nil, err
	}
	var granted []string
	cleanup = func() {
		for _, p := range granted {
			_ = revokeRead(p, uid)
		}
	}
	fail := func(err error) (func(), error) { cleanup(); return nil, err }
	if l.jailed {
		for src, name := range map[string]string{f.Kernel: "vmlinux", f.Rootfs: "rootfs.ext4"} {
			if err := linkOrCopy(src, l.file(name)); err != nil {
				return fail(fmt.Errorf("%s into jail: %w", name, err))
			}
			if !readableBy(l.file(name), uid) {
				if err := grantRead(l.file(name), uid); err != nil {
					return fail(err)
				}
				granted = append(granted, l.file(name))
			}
		}
		for _, d := range []string{"run", "snap"} {
			if err := os.MkdirAll(l.file(d), 0o700); err != nil {
				return fail(err)
			}
			_ = os.Chown(l.file(d), uid, uid)
		}
	}
	if f.OverlayMiB > 0 {
		src := ""
		if snap != nil {
			src = snap.OverlayPath()
		} else if src, err = f.overlayTemplate(); err != nil {
			return fail(err)
		}
		if err := copySparseFast(src, l.file("overlay.ext4")); err != nil {
			return fail(fmt.Errorf("overlay: %w", err))
		}
		if l.jailed {
			_ = os.Chown(l.file("overlay.ext4"), uid, uid)
		}
	}
	if snap != nil {
		if err := os.MkdirAll(l.file("restore"), 0o700); err != nil {
			return fail(err)
		}
		for _, name := range []string{"mem", "state"} {
			src, dst := filepath.Join(snap.Dir, name), filepath.Join(l.file("restore"), name)
			if err := os.Link(src, dst); err != nil {
				// Another filesystem: a private sparse copy (no page sharing).
				if err := copySparseFast(src, dst); err != nil {
					return fail(fmt.Errorf("snapshot %s into jail: %w", name, err))
				}
				if l.jailed {
					_ = os.Chown(dst, uid, uid)
				}
				continue
			}
			if l.jailed && !readableBy(dst, uid) {
				if err := grantRead(src, uid); err != nil {
					return fail(fmt.Errorf("snapshot %s readable by vm uid: %w", name, err))
				}
				granted = append(granted, src)
			}
		}
		if l.jailed {
			_ = os.Chown(l.file("restore"), uid, uid)
		}
	}
	if l.jailed {
		if err := os.Chown(l.host, uid, uid); err != nil {
			return fail(fmt.Errorf("chown jail root: %w", err))
		}
		_ = os.Chmod(l.host, 0o700)
	}
	return cleanup, nil
}

func linkOrCopy(src, dst string) error {
	_ = os.Remove(dst)
	if err := os.Link(src, dst); err == nil {
		return nil
	}
	return copySparseFast(src, dst)
}

// overlayTemplate is one empty ext4 image per host, made once with mke2fs and
// copied sparse per VM (a few MB of metadata, not OverlayMiB).
func (f *Firecracker) overlayTemplate() (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	st := f.store()
	if err := os.MkdirAll(st.Dir, 0o700); err != nil {
		return "", err
	}
	path := filepath.Join(st.Dir, fmt.Sprintf("overlay-template-%dM.ext4", f.OverlayMiB))
	if _, err := os.Stat(path); err == nil {
		return path, nil
	}
	tmp := path + ".tmp"
	_ = os.Remove(tmp)
	fh, err := os.OpenFile(tmp, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		return "", err
	}
	if err := fh.Truncate(int64(f.OverlayMiB) << 20); err != nil {
		fh.Close()
		return "", err
	}
	fh.Close()
	out, err := exec.Command(f.mke2fs(), "-q", "-F", "-t", "ext4", "-O", "^has_journal", "-E", "lazy_itable_init=1",
		"-m", "0", "-L", "boss-overlay", tmp).CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("mke2fs: %v: %s", err, strings.TrimSpace(string(out)))
	}
	return path, os.Rename(tmp, path)
}

// ---- lifecycle ------------------------------------------------------------

func (f *Firecracker) Create(ctx context.Context, s Spec) (*VM, error) {
	return f.launch(ctx, s, nil)
}

// Restore starts a VM from a snapshot of its shape (LLD §3.3 "restoring").
func (f *Firecracker) Restore(ctx context.Context, s Spec, snap SnapshotRef) (*VM, error) {
	if snap.Dir == "" {
		return nil, ErrNoSnapshot
	}
	if got := f.shapeOf(s).Shape(); got != snap.Shape {
		return nil, fmt.Errorf("snapshot %s is for another shape (%+v vs %+v)", snap.Key, snap.Shape, got)
	}
	return f.launch(ctx, s, &snap)
}

func (f *Firecracker) launch(ctx context.Context, s Spec, snap *SnapshotRef) (*VM, error) {
	if !VMIDPattern.MatchString(s.VMID) {
		return nil, fmt.Errorf("invalid vm_id %q", s.VMID)
	}
	if err := f.Check(); err != nil {
		return nil, err
	}
	s = f.shapeOf(s)
	l := f.layoutFor(s.VMID)
	fcid := FirecrackerID(s.VMID)
	uid := 0
	if l.jailed {
		uid = f.Jailer.uidFor(fcid)
	}
	cleanup, err := f.prepare(s, l, uid, snap)
	if err != nil {
		f.Jailer.release(uid)
		return nil, err
	}
	var netnsPathArg string
	if l.jailed && !f.Jailer.NoNetns {
		ns := netnsName(fcid)
		if s.Net != nil {
			if err := ensureReplicaLink(ns, s.Net.Tap, uid); err != nil {
				cleanup()
				_ = deleteNetns(ns)
				return nil, fmt.Errorf("replica link: %w", err)
			}
		} else if err := createNetns(ns); err != nil {
			cleanup()
			return nil, fmt.Errorf("netns for %s: %w", fcid, err)
		}
		netnsPathArg = netnsPath(ns)
	}
	var argv []string
	if snap == nil {
		raw, err := json.MarshalIndent(f.config(s, l), "", "  ")
		if err != nil {
			cleanup()
			return nil, err
		}
		if err := os.WriteFile(l.file("vm.json"), raw, 0o600); err != nil {
			cleanup()
			return nil, err
		}
		if l.jailed {
			_ = os.Chown(l.file("vm.json"), uid, uid)
		}
	}
	if l.jailed {
		var fcArgs []string
		if snap == nil {
			fcArgs = []string{"--config-file", l.guest("vm.json")}
		}
		argv = f.Jailer.argv(fcid, f.Bin, uid, s.VCPU, s.MemMiB, netnsPathArg, fcArgs...)
	} else {
		argv = append(append([]string{}, f.Launcher...), f.Bin, "--api-sock", l.apiSock(), "--id", fcid)
		if snap == nil {
			argv = append(argv, "--config-file", l.file("vm.json"))
		}
	}
	console, err := os.OpenFile(filepath.Join(l.meta, "console.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		cleanup()
		return nil, err
	}
	defer console.Close()
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Stdout, cmd.Stderr = console, console
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		cleanup()
		return nil, err
	}
	vm := newVM(s.VMID, cmd.Process.Pid, l.host, false)
	vm.Meta, vm.UID, vm.Jailed = l.meta, uid, l.jailed
	vm.Restored, vm.Snapshot, vm.revoke = snap != nil, snap, cleanup
	sp := s.Normalized()
	vm.Spec = &sp
	_ = writeSpec(l.meta, s)

	deadline := time.Now().Add(10 * time.Second)
	if l.jailed {
		// With --new-pid-ns the jailer forks Firecracker into the new
		// namespace, writes its pid file and exits at once; Firecracker is
		// not our child. Wait for the pid file, then watch that pid.
		wrapper := make(chan error, 1)
		go func() { wrapper <- cmd.Wait() }()
		pid, err := f.waitPidFile(fcid, deadline, wrapper)
		if err != nil {
			vm.PID = 0
			_ = f.Stop(context.Background(), vm)
			return nil, fmt.Errorf("jailer start: %w: %s", err, tail(filepath.Join(l.meta, "console.log")))
		}
		vm.PID = pid
		watchAdopted(vm)
	} else {
		go func() { _ = cmd.Wait(); vm.markExited() }()
	}
	if err := waitPath(vm, l.apiSock(), deadline); err != nil {
		_ = f.Stop(context.Background(), vm)
		return nil, fmt.Errorf("firecracker api socket: %w: %s", err, tail(filepath.Join(l.meta, "console.log")))
	}
	if snap != nil {
		api := fcAPI{sock: l.apiSock()}
		if err := api.loadSnapshot(ctx, l.guest("restore/state"), l.guest("restore/mem")); err != nil {
			_ = f.Stop(context.Background(), vm)
			return nil, fmt.Errorf("restore %s: %w: %s", snap.Key, err, tail(filepath.Join(l.meta, "console.log")))
		}
	}
	if err := waitPath(vm, l.file("v.sock"), deadline); err != nil {
		_ = f.Stop(context.Background(), vm)
		return nil, fmt.Errorf("vsock: %w: %s", err, tail(filepath.Join(l.meta, "console.log")))
	}
	return vm, nil
}

func (f *Firecracker) waitPidFile(fcid string, deadline time.Time, wrapper <-chan error) (int, error) {
	exited := false
	for time.Now().Before(deadline) {
		if raw, err := os.ReadFile(f.Jailer.pidFile(fcid)); err == nil {
			if pid, err := strconv.Atoi(strings.TrimSpace(string(raw))); err == nil && pid > 0 && procscan.Alive(pid) {
				return pid, nil
			}
		}
		if exited {
			return 0, errors.New("jailer exited without a live firecracker pid")
		}
		select {
		case err := <-wrapper:
			if err != nil {
				return 0, fmt.Errorf("jailer failed: %w", err)
			}
			exited = true // normal: it wrote the pid file and left
		case <-time.After(10 * time.Millisecond):
		}
	}
	return 0, errors.New("no pid file from the jailer")
}

func waitPath(vm *VM, path string, deadline time.Time) error {
	for time.Now().Before(deadline) {
		if _, err := os.Stat(path); err == nil {
			return nil
		}
		select {
		case <-vm.Exited:
			return errors.New("firecracker exited during start")
		case <-time.After(5 * time.Millisecond):
		}
	}
	return fmt.Errorf("%s did not appear", filepath.Base(path))
}

func tail(path string) string {
	raw, _ := os.ReadFile(path)
	if len(raw) > 600 {
		raw = raw[len(raw)-600:]
	}
	return strings.TrimSpace(string(raw))
}

// Snapshot pauses a clean VM, writes a full snapshot (memory + device state)
// and the overlay into the store under the VM's shape, and resumes it.
func (f *Firecracker) Snapshot(ctx context.Context, vm *VM) (SnapshotRef, error) {
	if tainted, why := vm.Tainted(); tainted {
		return SnapshotRef{}, fmt.Errorf("%w (%s)", ErrTainted, why)
	}
	if vm.Spec == nil {
		return SnapshotRef{}, errors.New("snapshot: VM has no spec record")
	}
	if vm.Spec.Net != nil && !vm.Jailed {
		// The snapshot bakes the NIC's host_dev_name and Firecracker 1.10 has
		// no network_overrides at load (1.12+): a restored replica would open
		// the template's tap. Only jailed replicas (uniform tap0 in their own
		// netns) can be snapshotted; unjailed replicas cold boot every time.
		return SnapshotRef{}, ErrReplicaNeedsJail
	}
	spec := f.shapeOf(*vm.Spec)
	shape := spec.Shape()
	st := f.store()
	if err := os.MkdirAll(st.Dir, 0o700); err != nil {
		return SnapshotRef{}, err
	}
	dir, err := st.Begin(shape)
	if err != nil {
		return SnapshotRef{}, err
	}
	l := layout{host: vm.Dir, meta: vm.Meta, jailed: vm.Jailed}
	api := fcAPI{sock: l.apiSock()}
	fail := func(err error) (SnapshotRef, error) { _ = os.RemoveAll(dir); return SnapshotRef{}, err }
	if err := os.MkdirAll(l.file("snap"), 0o700); err != nil {
		return fail(err)
	}
	if l.jailed {
		_ = os.Chown(l.file("snap"), vm.UID, vm.UID)
	}
	_ = os.Remove(l.file("snap/mem"))
	_ = os.Remove(l.file("snap/state"))
	if err := api.pause(ctx); err != nil {
		return fail(err)
	}
	if err := api.createSnapshot(ctx, l.guest("snap/state"), l.guest("snap/mem")); err != nil {
		_ = api.resume(ctx)
		return fail(err)
	}
	// The overlay is copied while paused, so disk and memory agree.
	if f.OverlayMiB > 0 {
		if err := copySparseFast(l.file("overlay.ext4"), filepath.Join(dir, "overlay.ext4")); err != nil {
			_ = api.resume(ctx)
			return fail(err)
		}
	}
	if err := api.resume(ctx); err != nil {
		return fail(err)
	}
	var memBytes int64
	for _, name := range []string{"mem", "state"} {
		src, dst := l.file("snap/"+name), filepath.Join(dir, name)
		if err := moveFile(src, dst); err != nil {
			return fail(fmt.Errorf("store %s: %w", name, err))
		}
		_ = os.Chown(dst, os.Geteuid(), os.Getegid())
		_ = os.Chmod(dst, 0o600)
		if name == "mem" {
			if st, err := os.Stat(dst); err == nil {
				memBytes = st.Size()
			}
		}
	}
	ref := SnapshotRef{Key: shape.Key(), Shape: shape, Dir: dir, CleanAttested: true, CreatedAt: time.Now().UTC(),
		SourceVMID: vm.ID, MemBytes: memBytes, Backend: f.Name()}
	if err := st.Commit(ref); err != nil {
		return fail(err)
	}
	return ref, nil
}

func (f *Firecracker) FindSnapshot(shape Shape) (SnapshotRef, bool) {
	if shape.KernelDigest == "" {
		shape.KernelDigest = f.KernelDigest()
	}
	if shape.RuntimeVersion == "" {
		shape.RuntimeVersion = f.RuntimeVersion
	}
	return f.store().Find(shape)
}

func (f *Firecracker) Snapshots() []SnapshotRef             { return f.store().List() }
func (f *Firecracker) DeleteSnapshot(ref SnapshotRef) error { return f.store().Delete(ref) }

func (f *Firecracker) Stop(ctx context.Context, vm *VM) error {
	err := stopProcess(ctx, vm, 500*time.Millisecond)
	if err != nil {
		return err
	}
	if vm.revoke != nil {
		vm.revoke()
	} else if vm.Jailed && vm.UID > 0 {
		// Adopted: revoke whatever this uid may read in the store.
		for _, ref := range f.store().List() {
			_ = revokeRead(ref.MemPath(), vm.UID)
			_ = revokeRead(ref.StatePath(), vm.UID)
		}
		_ = revokeRead(f.Kernel, vm.UID)
		_ = revokeRead(f.Rootfs, vm.UID)
	}
	if vm.Jailed && f.Jailer != nil {
		fcid := filepath.Base(filepath.Dir(vm.Dir))
		f.Jailer.release(vm.UID)
		if vm.Meta != "" && strings.HasPrefix(vm.Meta, f.Jailer.ChrootBase) {
			_ = os.RemoveAll(vm.Meta)
		}
		for i := 0; i < 20; i++ {
			if err := os.Remove(f.Jailer.cgroupDir(fcid)); err == nil || errors.Is(err, os.ErrNotExist) {
				break
			}
			time.Sleep(50 * time.Millisecond)
		}
		if f.Jailer.NoNetns == false {
			_ = deleteNetns(netnsName(fcid)) // a replica's veth pair dies with its host end (netpol.Remove)
		}
		return nil
	}
	if vm.Dir != "" && f.Root != "" && strings.HasPrefix(vm.Dir, f.Root) {
		_ = os.RemoveAll(vm.Dir)
	}
	return nil
}

// OwnsReplicaLink implements LinkOwner: under the jailer the VM's netns and
// its veth/bridge/tap are built at Create; netpol applies policy afterwards.
func (f *Firecracker) OwnsReplicaLink() bool { return f.Jailer != nil && !f.Jailer.NoNetns }

// SweepNetns deletes namespaces this backend made for VMs that no longer
// exist (left by a crash): a VM's jail directory is created before its
// namespace and removed at Stop, so a namespace without a jail is an orphan.
// live (running VM ids) is kept as a second guard for VMs mid-start.
func (f *Firecracker) SweepNetns(live map[string]bool) {
	ents, err := os.ReadDir("/run/netns")
	if err != nil || f.Jailer == nil {
		return
	}
	for _, e := range ents {
		if !strings.HasPrefix(e.Name(), "bv-") {
			continue
		}
		fcid := strings.TrimPrefix(e.Name(), "bv-")
		if live[fcid] {
			continue
		}
		if _, err := os.Stat(f.Jailer.jailDir(fcid)); err == nil {
			continue
		}
		_ = deleteNetns(e.Name())
	}
}

func (f *Firecracker) Stats(vm *VM) Stats { return procStats(vm.PID) }

// Scan finds firecracker processes of this host: jailed ones by the --id the
// jailer gave them and the pid file in that jail (the jailer pivots the root,
// so /proc/<pid>/root reads "/"), unjailed ones by an API socket under Root.
func (f *Firecracker) Scan() ([]*VM, error) {
	procs, err := procscan.Find(func(pr procscan.Proc) bool {
		return len(pr.Args) > 0 && filepath.Base(pr.Args[0]) == "firecracker"
	})
	if err != nil {
		return nil, err
	}
	var out []*VM
	for _, pr := range procs {
		var vm *VM
		if f.Jailer != nil && pr.Flag("api-sock") == "" {
			fcid := pr.Flag("id")
			if raw, err := os.ReadFile(f.Jailer.pidFile(fcid)); err == nil && strings.TrimSpace(string(raw)) == strconv.Itoa(pr.PID) {
				vm = newVM(fcid, pr.PID, f.Jailer.chroot(fcid), true)
				vm.Meta, vm.Jailed, vm.UID = f.Jailer.jailDir(fcid), true, procscan.UID(pr.PID)
				vm.Spec = readSpec(vm.Meta)
			}
		}
		if vm == nil {
			sock := pr.Flag("api-sock")
			if f.Root == "" || !strings.HasPrefix(sock, f.Root+string(os.PathSeparator)) || pr.Flag("id") == "" {
				continue
			}
			dir := filepath.Dir(sock)
			vm = newVM(filepath.Base(dir), pr.PID, dir, true) // the dir name is the vm id
			vm.Spec = readSpec(dir)
		}
		if vm.Spec != nil && vm.Spec.VMID != "" {
			vm.ID = vm.Spec.VMID // a claimed warm VM is known by its control-plane id
		}
		watchAdopted(vm)
		out = append(out, vm)
	}
	return out, nil
}

// DialControl uses Firecracker's host-initiated vsock handshake:
// "CONNECT <port>\n" → "OK <host port>\n" on the VM's Unix socket. The
// whole exchange (dial + handshake) is bounded by deadline.
func (f *Firecracker) DialControl(vm *VM, deadline time.Time) (net.Conn, error) {
	return f.dialVsock(vm, guest.PortControl, deadline)
}

// DialInbox opens the guest's work inbox (vsock port 7105).
func (f *Firecracker) DialInbox(vm *VM, deadline time.Time) (net.Conn, error) {
	return f.dialVsock(vm, guest.PortInbox, deadline)
}

func (f *Firecracker) dialVsock(vm *VM, port int, deadline time.Time) (net.Conn, error) {
	c, err := (&net.Dialer{Deadline: deadline}).Dial("unix", filepath.Join(vm.Dir, "v.sock"))
	if err != nil {
		return nil, err
	}
	_ = c.SetDeadline(deadline)
	if _, err := c.Write([]byte("CONNECT " + strconv.Itoa(port) + "\n")); err != nil {
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
	return c, nil // the deadline stays: callers do one exchange per connection
}

// GuestListen listens on <uds>_<port>, where Firecracker forwards
// guest-initiated vsock connections to that port. Under the jailer the
// socket is handed to the VM uid, which is the only thing allowed to connect.
func (f *Firecracker) GuestListen(vm *VM, port int) (net.Listener, error) {
	path := filepath.Join(vm.Dir, "v.sock_"+strconv.Itoa(port))
	ln, err := listenUnix(path)
	if err != nil {
		return nil, err
	}
	if vm.Jailed && vm.UID > 0 {
		_ = os.Chown(path, vm.UID, vm.UID)
	}
	return ln, nil
}
