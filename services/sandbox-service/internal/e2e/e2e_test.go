// Package e2e runs the real boss-hostd binary against the Go fake control
// plane. The process backend runs everywhere (laptop/CI); set
// BOSS_E2E_BACKEND=firecracker (plus the BOSS_E2E_FC_* variables) on a KVM
// host such as boss-1 to run the same tests on real microVMs.
//
//	BOSS_E2E_BACKEND        process (default) | firecracker
//	BOSS_E2E_FC_BIN         firecracker binary
//	BOSS_E2E_FC_KERNEL      vmlinux
//	BOSS_E2E_FC_ROOTFS      base rootfs with /sbin/boss-vsockd baked in
//	BOSS_E2E_NETADM_SOCKET  socket of a running `boss-hostd netadm` helper
//	BOSS_E2E_RUN_DIR        short per-VM dir root (Unix socket path limit)
//	BOSS_E2E_REPLICAS       replica count for T4 (default 20)
package e2e

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/fakecp"
	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanheiney/boss/services/sandbox-service/internal/vmm"
)

type env struct {
	t        *testing.T
	bin      string // boss-hostd
	guest    string // boss-vsockd
	cp       *fakecp.CP
	root     string
	state    string
	runDir   string
	backend  string
	hostd    *exec.Cmd
	hostdLog string
	mgr      vmm.Manager
}

func buildBinaries(t *testing.T, out string) (string, string) {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	mod := filepath.Join(filepath.Dir(file), "..", "..")
	if pre := os.Getenv("BOSS_E2E_BIN_DIR"); pre != "" {
		return filepath.Join(pre, "boss-hostd"), filepath.Join(pre, "boss-vsockd")
	}
	for _, name := range []string{"boss-hostd", "boss-vsockd"} {
		cmd := exec.Command("go", "build", "-o", filepath.Join(out, name), "./cmd/"+name)
		cmd.Dir = mod
		cmd.Env = append(os.Environ(), "CGO_ENABLED=0")
		if b, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("build %s: %v\n%s", name, err, b)
		}
	}
	return filepath.Join(out, "boss-hostd"), filepath.Join(out, "boss-vsockd")
}

func newEnv(t *testing.T) *env {
	t.Helper()
	if testing.Short() {
		t.Skip("e2e builds binaries; skipped in -short")
	}
	root, err := os.MkdirTemp("", "be")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(root) })
	e := &env{t: t, root: root, state: filepath.Join(root, "s"), backend: os.Getenv("BOSS_E2E_BACKEND")}
	if e.backend == "" {
		e.backend = "process"
	}
	e.runDir = os.Getenv("BOSS_E2E_RUN_DIR")
	if e.runDir == "" {
		e.runDir = filepath.Join(root, "v")
	}
	if err := os.MkdirAll(e.runDir, 0o700); err != nil {
		t.Fatal(err)
	}
	e.bin, e.guest = buildBinaries(t, root)
	switch e.backend {
	case "process":
		e.mgr = &vmm.Process{Root: e.runDir, GuestBin: e.guest}
	case "firecracker":
		e.mgr = &vmm.Firecracker{Root: e.runDir, Bin: os.Getenv("BOSS_E2E_FC_BIN"),
			Kernel: os.Getenv("BOSS_E2E_FC_KERNEL"), Rootfs: os.Getenv("BOSS_E2E_FC_ROOTFS")}
		if err := e.mgr.Check(); err != nil {
			t.Skipf("firecracker not ready: %v", err)
		}
	default:
		t.Fatalf("unknown BOSS_E2E_BACKEND %q", e.backend)
	}
	cp, err := fakecp.New()
	if err != nil {
		t.Fatal(err)
	}
	if err := cp.Start("127.0.0.1:0"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(cp.Close)
	e.cp = cp
	if err := os.WriteFile(filepath.Join(root, "ca.pem"), []byte(cp.CA.PEM), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "join"), []byte("join-e2e\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cp.AddJoinToken("join-e2e")
	t.Cleanup(e.cleanup)
	return e
}

func (e *env) startHostd() {
	e.t.Helper()
	e.hostdLog = filepath.Join(e.root, "hostd.log")
	logf, _ := os.OpenFile(e.hostdLog, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	cmd := exec.Command(e.bin)
	cmd.Env = append(os.Environ(),
		"BOSS_SANDBOX_LISTEN=127.0.0.1:0",
		"BOSS_HOSTD_CP_URL="+e.cp.URL,
		"BOSS_HOSTD_CP_CA="+filepath.Join(e.root, "ca.pem"),
		"BOSS_HOSTD_JOIN_TOKEN_FILE="+filepath.Join(e.root, "join"),
		"BOSS_HOSTD_STATE_DIR="+e.state,
		"BOSS_HOSTD_RUN_DIR="+e.runDir,
		"BOSS_HOSTD_VM_BACKEND="+e.backend,
		"BOSS_HOSTD_GUEST_BIN="+e.guest,
		"BOSS_SANDBOX_FC_BIN="+os.Getenv("BOSS_E2E_FC_BIN"),
		"BOSS_SANDBOX_FC_KERNEL="+os.Getenv("BOSS_E2E_FC_KERNEL"),
		"BOSS_HOSTD_FC_ROOTFS="+os.Getenv("BOSS_E2E_FC_ROOTFS"),
		"BOSS_HOSTD_NETADM_SOCKET="+os.Getenv("BOSS_E2E_NETADM_SOCKET"),
		"BOSS_HOSTD_MAX_VMS=80",
		"BOSS_MICROVM_CLAIMS_FROZEN="+envOr("BOSS_E2E_FROZEN", "true"),
	)
	cmd.Stdout, cmd.Stderr = logf, logf
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		e.t.Fatal(err)
	}
	e.hostd = cmd
	go func() { _ = cmd.Wait() }()
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func (e *env) kill9() {
	_ = syscall.Kill(e.hostd.Process.Pid, syscall.SIGKILL)
	time.Sleep(100 * time.Millisecond)
}

func (e *env) cleanup() {
	if e.hostd != nil && e.hostd.Process != nil {
		_ = syscall.Kill(e.hostd.Process.Pid, syscall.SIGKILL)
	}
	// Desired = nothing would stop VMs through hostd; the test owns cleanup
	// directly so a failed run never leaves VMs or taps behind.
	if vms, err := e.mgr.Scan(); err == nil {
		for _, vm := range vms {
			_ = e.mgr.Stop(context.Background(), vm)
		}
	}
	removeTaps(e)
}

func (e *env) log() string {
	raw, _ := os.ReadFile(e.hostdLog)
	if len(raw) > 6000 {
		raw = raw[len(raw)-6000:]
	}
	return string(raw)
}

func (e *env) hostID() string {
	ids := e.cp.HostIDs()
	if len(ids) == 0 {
		return ""
	}
	return ids[0]
}

func (e *env) waitFor(d time.Duration, what string, cond func() bool) time.Duration {
	e.t.Helper()
	start := time.Now()
	ctx, cancel := context.WithTimeout(context.Background(), d)
	defer cancel()
	if !fakecp.WaitFor(ctx, 50*time.Millisecond, cond) {
		e.t.Fatalf("timed out after %s waiting for %s\nhostd log tail:\n%s", d, what, e.log())
	}
	return time.Since(start)
}

// servingSince reports the VMs a heartbeat sent after `after` shows serving.
func (e *env) servingSince(after time.Time) map[string]hostapi.ReplicaStatus {
	hb, at := e.cp.LastHeartbeat(e.hostID())
	out := map[string]hostapi.ReplicaStatus{}
	if hb == nil || at.Before(after) {
		return out
	}
	for _, v := range hb.Replicas {
		if v.State == hostapi.StateServing {
			out[v.VMID] = v
		}
	}
	return out
}

func (e *env) pids() map[string][]int {
	vms, err := e.mgr.Scan()
	if err != nil {
		e.t.Fatal(err)
	}
	out := map[string][]int{}
	for _, v := range vms {
		out[v.ID] = append(out[v.ID], v.PID)
	}
	return out
}

// desired is shaped exactly like fleet.desired() (vm ids "vm_…", replica ids
// "rep_…", flat image_digest, resources JSON), plus the P8b directors list.
func desired(n int, prefix string, frozen bool, endpoints map[string]string) hostapi.DesiredState {
	base := strings.TrimSuffix(strings.Split(prefix, "/")[0], ".0")
	ds := hostapi.DesiredState{ClaimsFrozen: frozen, Endpoints: endpoints, Directors: []string{"192.0.2.10"}}
	for i := 1; i <= n; i++ {
		ds.VMs = append(ds.VMs, hostapi.DesiredVM{
			ReplicaID: fmt.Sprintf("rep_%02d", i), VMID: fmt.Sprintf("vm_%02d", i), State: hostapi.DesiredRunning,
			DeploymentID: "agent-x", AgentVersion: "v1", Mode: hostapi.ModeReplica, ImageDigest: "sha256:p8a-agent",
			Resources: hostapi.Resources{"vcpu": 1.0, "mem_mib": 128.0}, Slots: 4, Weight: 1,
			VMIP: fmt.Sprintf("%s.%d", base, 10+i), VIP: "203.0.113.17", VIPPorts: []int{443}, StrictSecrets: true,
		})
	}
	return ds
}

// P8a-T2: replicas: 3 converges. kill -9 boss-hostd plus deleting the bbolt
// file converges again within 10 s, with no duplicate VMs and the running
// VMs untouched.
func TestP8aT2KillDashNineAndCacheLoss(t *testing.T) {
	e := newEnv(t)
	e.startHostd()
	e.waitFor(10*time.Second, "enrolment", func() bool { return e.hostID() != "" })
	prefix := "10.200.4.0/23"
	e.cp.SetDesired(e.hostID(), desired(3, prefix, true, nil))
	took := e.waitFor(60*time.Second, "3 serving replicas", func() bool { return len(e.servingSince(time.Time{})) == 3 })
	t.Logf("backend=%s: replicas:3 converged in %s", e.backend, took)
	before := e.pids()

	e.kill9()
	if err := os.Remove(filepath.Join(e.state, "cache.db")); err != nil {
		t.Fatalf("delete bbolt: %v", err)
	}
	restart := time.Now()
	e.startHostd()
	took = e.waitFor(10*time.Second, "reconverge after kill -9 + bbolt loss", func() bool {
		return len(e.servingSince(restart)) == 3
	})
	after := e.pids()
	if len(after) != 3 {
		t.Fatalf("VMs after restart: %v", after)
	}
	for id, p := range after {
		if len(p) != 1 {
			t.Fatalf("duplicate VM processes for %s: %v", id, p)
		}
		if before[id][0] != p[0] {
			t.Fatalf("VM %s restarted (%v → %v): running VMs must be untouched", id, before[id], p)
		}
	}
	t.Logf("backend=%s: reconverged %s after kill -9 + bbolt delete (same PIDs, no duplicates)", e.backend, took)
	adopted := 0
	for _, ev := range e.cp.Events() {
		if ev.Kind == "adopted" {
			adopted++
		}
	}
	if adopted != 3 {
		t.Fatalf("want 3 adopted events, got %d", adopted)
	}
}
