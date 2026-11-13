package backend

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

// Firecracker boots one microVM per task:
//
//	kernel (vmlinux) + per-task writable copy of the rootfs (ext4, baked by
//	scripts/sandbox/microvm-setup.sh with python3/git/node, the in-guest agent
//	at /opt/boss/in_guest_agent.py and /sbin/boss-init as init) + a per-task
//	"work" ext4 drive carrying spec.json in and result.json/agent.log out.
//
// boss-init mounts the work drive at /work, runs the agent, and powers off, so
// one `firecracker --no-api --config-file vm.json` process is the whole VM
// lifetime. The host never mounts guest images (no root needed): the work
// drive is built with `mke2fs -d` and read back with `debugfs`.
//
// Artifacts this backend needs on the host (see docs/infra/MICROVM_ENABLE.md):
//   - /dev/kvm (rw for the service user)
//   - the firecracker binary        (BOSS_SANDBOX_FC_BIN)
//   - an uncompressed kernel image  (BOSS_SANDBOX_FC_KERNEL, vmlinux)
//   - the baked rootfs ext4 image   (BOSS_SANDBOX_FC_ROOTFS)
//   - e2fsprogs (mke2fs + debugfs) on PATH
type Firecracker struct {
	Bin       string // firecracker binary
	Kernel    string // vmlinux path
	Rootfs    string // base rootfs ext4 (copied per task, never written)
	Vcpus     int    // default 2
	MemMB     int    // default 2048
	WorkMB    int    // size of the per-task work drive; default 2048
	TapDev    string // optional host tap device for guest networking
	GuestIP   string // optional, with TapDev: e.g. 172.16.0.2
	GatewayIP string // optional, with TapDev: e.g. 172.16.0.1
}

func (f *Firecracker) Name() string { return "firecracker" }

// Check names exactly which piece is missing — the error string is what the
// 422 submit rejection and the health `reason` carry.
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
		return fmt.Errorf("firecracker backend: rootfs image missing at %s (run scripts/sandbox/microvm-setup.sh)", f.Rootfs)
	}
	for _, tool := range []string{"mke2fs", "debugfs"} {
		if _, err := lookPathSbin(tool); err != nil {
			return fmt.Errorf("firecracker backend: %s not found (install e2fsprogs)", tool)
		}
	}
	return nil
}

// lookPathSbin finds a tool on PATH, falling back to the sbin dirs that are
// often not on a service user's PATH.
func lookPathSbin(tool string) (string, error) {
	if p, err := exec.LookPath(tool); err == nil {
		return p, nil
	}
	for _, dir := range []string{"/usr/sbin", "/sbin", "/usr/local/sbin"} {
		p := filepath.Join(dir, tool)
		if info, err := os.Stat(p); err == nil && !info.IsDir() {
			return p, nil
		}
	}
	return "", fmt.Errorf("%s not found", tool)
}

func (f *Firecracker) vcpus() int {
	if f.Vcpus > 0 {
		return f.Vcpus
	}
	return 2
}
func (f *Firecracker) memMB() int {
	if f.MemMB > 0 {
		return f.MemMB
	}
	return 2048
}
func (f *Firecracker) workMB() int {
	if f.WorkMB > 0 {
		return f.WorkMB
	}
	return 2048
}

func runTool(ctx context.Context, logPath string, name string, args ...string) error {
	cmd := exec.CommandContext(ctx, name, args...)
	out, err := cmd.CombinedOutput()
	if logPath != "" && len(out) > 0 {
		fd, ferr := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
		if ferr == nil {
			fmt.Fprintf(fd, "$ %s %s\n%s\n", name, strings.Join(args, " "), out)
			fd.Close()
		}
	}
	if err != nil {
		return fmt.Errorf("%s: %v: %s", name, err, strings.TrimSpace(string(out)))
	}
	return nil
}

func (f *Firecracker) Run(ctx context.Context, r RunRequest) error {
	if err := f.Check(); err != nil {
		return err
	}
	runCtx := ctx
	if r.Timeout > 0 {
		var cancel context.CancelFunc
		runCtx, cancel = context.WithTimeout(ctx, r.Timeout)
		defer cancel()
	}
	setupLog := filepath.Join(r.TaskDir, "setup.log")

	// 1. Per-task writable rootfs copy (sparse — the base image stays pristine).
	taskRootfs := filepath.Join(r.TaskDir, "rootfs.ext4")
	if err := runTool(runCtx, setupLog, "cp", "--sparse=always", f.Rootfs, taskRootfs); err != nil {
		return fmt.Errorf("firecracker backend: copy rootfs: %w", err)
	}

	// 2. Work drive carrying spec.json in (and result.json/agent.log out).
	stage := filepath.Join(r.TaskDir, "workstage")
	if err := os.MkdirAll(stage, 0o755); err != nil {
		return err
	}
	spec, err := os.ReadFile(r.SpecPath)
	if err != nil {
		return fmt.Errorf("firecracker backend: read spec: %w", err)
	}
	if err := os.WriteFile(filepath.Join(stage, "spec.json"), spec, 0o644); err != nil {
		return err
	}
	workImg := filepath.Join(r.TaskDir, "work.ext4")
	mke2fs, _ := lookPathSbin("mke2fs")
	if err := runTool(runCtx, setupLog, mke2fs, "-F", "-q", "-t", "ext4", "-m", "0",
		"-L", "bosswork", "-d", stage, workImg, fmt.Sprintf("%dm", f.workMB())); err != nil {
		return fmt.Errorf("firecracker backend: build work drive: %w", err)
	}

	// 3. VM config + boot.
	bootArgs := "console=ttyS0 reboot=k panic=1 pci=off random.trust_cpu=on init=/sbin/boss-init"
	var netIfaces []map[string]any
	if f.TapDev != "" && f.GuestIP != "" && f.GatewayIP != "" {
		bootArgs += fmt.Sprintf(" ip=%s::%s:255.255.255.252::eth0:off", f.GuestIP, f.GatewayIP)
		netIfaces = append(netIfaces, map[string]any{
			"iface_id":      "eth0",
			"guest_mac":     "AA:FC:00:00:00:01",
			"host_dev_name": f.TapDev,
		})
	}
	cfg := map[string]any{
		"boot-source": map[string]any{
			"kernel_image_path": f.Kernel,
			"boot_args":         bootArgs,
		},
		"drives": []map[string]any{
			{"drive_id": "rootfs", "path_on_host": taskRootfs, "is_root_device": true, "is_read_only": false},
			{"drive_id": "work", "path_on_host": workImg, "is_root_device": false, "is_read_only": false},
		},
		"machine-config": map[string]any{
			"vcpu_count":   f.vcpus(),
			"mem_size_mib": f.memMB(),
		},
	}
	if len(netIfaces) > 0 {
		cfg["network-interfaces"] = netIfaces
	}
	cfgPath := filepath.Join(r.TaskDir, "vm.json")
	raw, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(cfgPath, raw, 0o644); err != nil {
		return err
	}

	console, err := os.Create(r.ConsoleLog)
	if err != nil {
		return err
	}
	defer console.Close()
	vm := exec.CommandContext(runCtx, f.Bin, "--no-api", "--config-file", cfgPath)
	vm.Stdout = console
	vm.Stderr = console
	vmErr := vm.Run()
	if runCtx.Err() != nil {
		return runCtx.Err() // wall-clock budget hit — runner records status=timeout
	}

	// 4. Pull result.json / agent.log out of the work drive (no mounts needed),
	// and the repo tree for console fs browsing (best effort).
	debugfs, _ := lookPathSbin("debugfs")
	extract := func(guestPath, hostPath string) error {
		return runTool(context.Background(), setupLog, debugfs, "-R",
			fmt.Sprintf("dump %s %s", guestPath, hostPath), workImg)
	}
	_ = extract("/agent.log", filepath.Join(r.TaskDir, "agent.log"))
	if err := extract("/result.json", r.ResultPath); err != nil || !fileNonEmpty(r.ResultPath) {
		if vmErr != nil {
			return fmt.Errorf("firecracker backend: VM exited: %v (no result.json; see console.log)", vmErr)
		}
		return fmt.Errorf("firecracker backend: VM exited without writing result.json (see console.log)")
	}
	workDir := filepath.Join(r.TaskDir, "work")
	if err := os.MkdirAll(workDir, 0o755); err == nil {
		_ = runTool(context.Background(), setupLog, debugfs, "-R",
			fmt.Sprintf("rdump /repo %s", workDir), workImg)
	}
	return nil
}

func fileNonEmpty(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Size() > 0
}
