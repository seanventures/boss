package hostd

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/fakecp"
	"github.com/seanheiney/boss/services/sandbox-service/internal/guest"
	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanheiney/boss/services/sandbox-service/internal/vmm"
)

// The test binary doubles as the guest (boss-vsockd in process mode) so the
// process backend runs the real relay and control server.
func TestMain(m *testing.M) {
	if os.Getenv("BOSS_TEST_GUEST") == "1" {
		dir := ""
		for _, a := range os.Args[1:] {
			if strings.HasPrefix(a, "--dir=") {
				dir = strings.TrimPrefix(a, "--dir=")
			}
		}
		d := &guest.Daemon{T: guest.UnixTransport{Dir: dir}}
		if err := d.Run(context.Background()); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		os.Exit(0)
	}
	os.Exit(m.Run())
}

type harness struct {
	t      *testing.T
	cp     *fakecp.CP
	root   string
	state  string
	runDir string
	mgr    *vmm.Process
	agent  *Agent
	cancel context.CancelFunc
	done   chan struct{}
	logs   *logBuf
}

type logBuf struct {
	mu sync.Mutex
	b  strings.Builder
}

func (l *logBuf) logf(format string, args ...any) {
	l.mu.Lock()
	fmt.Fprintf(&l.b, format+"\n", args...)
	l.mu.Unlock()
}

func (l *logBuf) String() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.String()
}

// shortTemp keeps Unix socket paths under the 104-byte macOS limit.
func shortTemp(t *testing.T) string {
	dir, err := os.MkdirTemp("", "bh")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	return dir
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	cp, err := fakecp.New()
	if err != nil {
		t.Fatal(err)
	}
	if err := cp.Start("127.0.0.1:0"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(cp.Close)
	root := shortTemp(t)
	h := &harness{t: t, cp: cp, root: root, state: filepath.Join(root, "s"), runDir: filepath.Join(root, "v")}
	if err := os.MkdirAll(h.runDir, 0o700); err != nil {
		t.Fatal(err)
	}
	h.mgr = &vmm.Process{Root: h.runDir, GuestBin: os.Args[0], GuestEnv: []string{"BOSS_TEST_GUEST=1"}}
	t.Cleanup(func() { h.killAllVMs() })
	return h
}

func (h *harness) config(join string) Config {
	h.logs = &logBuf{}
	return Config{
		StateDir: h.state, CPURL: h.cp.URL, PinnedCA: []byte(h.cp.CA.PEM), JoinToken: join,
		Hostname: "test-host", Manager: h.mgr, ClaimsFrozen: true, StrictSecrets: true, MaxVMs: 20,
		HeartbeatEvery: 100 * time.Millisecond, WatchWait: 2 * time.Second,
		ReconcileEvery: 100 * time.Millisecond, PingEvery: 100 * time.Millisecond,
		BootGrace: 3 * time.Second, Logf: h.logs.logf, WorkWait: time.Second,
	}
}

func (h *harness) start(cfg Config) {
	h.t.Helper()
	a, err := New(cfg)
	if err != nil {
		h.t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	h.agent, h.cancel, h.done = a, cancel, make(chan struct{})
	go func() {
		defer close(h.done)
		if err := a.Run(ctx); err != nil && ctx.Err() == nil {
			h.t.Errorf("agent.Run: %v", err)
		}
	}()
	h.waitFor(5*time.Second, "enrolment", func() bool { return a.HostID() != "" })
}

// crash stops the agent without stopping its VMs (what kill -9 leaves).
func (h *harness) crash() {
	h.cancel()
	<-h.done
}

func (h *harness) killAllVMs() {
	vms, _ := h.mgr.Scan()
	for _, vm := range vms {
		_ = h.mgr.Stop(context.Background(), vm)
	}
}

func (h *harness) waitFor(d time.Duration, what string, cond func() bool) {
	h.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), d)
	defer cancel()
	if !fakecp.WaitFor(ctx, 50*time.Millisecond, cond) {
		h.t.Fatalf("timed out after %s waiting for %s\nagent log:\n%s", d, what, h.logs.String())
	}
}

func (h *harness) serving() map[string]hostapi.ReplicaStatus {
	out := map[string]hostapi.ReplicaStatus{}
	for _, v := range h.agent.VMs() {
		if v.State == hostapi.StateServing {
			out[v.VMID] = v
		}
	}
	return out
}

// replicas is a desired state shaped exactly like fleet.desired() returns it.
func replicas(n int, endpoints map[string]string) hostapi.DesiredState {
	ds := hostapi.DesiredState{Endpoints: endpoints}
	for i := 1; i <= n; i++ {
		ds.VMs = append(ds.VMs, hostapi.DesiredVM{
			ReplicaID: fmt.Sprintf("rep_%d", i), VMID: fmt.Sprintf("vm_%d", i), State: hostapi.DesiredRunning,
			DeploymentID: "dep-x", AgentVersion: "v1", Mode: hostapi.ModeReplica, ImageDigest: "sha256:agent",
			Resources: hostapi.Resources{"vcpu": 1.0, "mem_mib": 128.0}, Slots: 4,
			VMIP: fmt.Sprintf("10.200.4.%d", 10+i), Weight: 1, StrictSecrets: true,
		})
	}
	return ds
}

// P8a-T1: enrol with a one-time join token; heartbeats are accepted with the
// hardening facts; after revocation the heartbeat (and renewal) is refused,
// and the host stops admitting work.
func TestP8aT1RevokedHostHeartbeatRefused(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("join-once")
	h.start(h.config("join-once"))
	id := h.agent.HostID()
	h.waitFor(5*time.Second, "accepted heartbeats", func() bool { return h.cp.HeartbeatCount(id) >= 2 })

	hb, _ := h.cp.LastHeartbeat(id)
	if hb.ContractVersion != hostapi.Version || hb.Backend != "process" || hb.BGPState != "none" {
		t.Fatalf("heartbeat shape: %+v", hb)
	}
	if hb.Facts.Jailer || hb.Facts.FirecrackerLive || hb.Facts.NetPolicyOK {
		t.Fatalf("process backend must not claim isolation facts: %+v", hb.Facts)
	}
	for _, k := range []string{"jailer_on", "firecracker_live", "net_policy_ok"} {
		if hb.Facts.Reasons[k] == "" {
			t.Fatalf("false fact %s carries no reason: %+v", k, hb.Facts.Reasons)
		}
	}
	if !hb.Facts.StrictSecrets || hb.Capacity.MaxVMs != 20 || hb.Allocatable.VMs != 20 {
		t.Fatalf("capacity/strict facts wrong: %+v %+v %+v", hb.Facts, hb.Capacity, hb.Allocatable)
	}
	// The wire names are the control plane's column names.
	raw, _ := json.Marshal(hb)
	for _, k := range []string{`"kvm_live"`, `"jailer_on"`, `"seccomp_default"`, `"replicas"`, `"allocatable"`, `"warm_pools"`} {
		if !strings.Contains(string(raw), k) {
			t.Fatalf("heartbeat lacks %s: %s", k, raw)
		}
	}

	// The join token is one-time: a second host cannot reuse it.
	h2 := newHarness(t)
	h2.cp = h.cp
	cfg2 := h2.config("join-once")
	a2, err := New(cfg2)
	if err != nil {
		t.Fatal(err)
	}
	if err := a2.Enrol(context.Background()); err == nil || !strings.Contains(err.Error(), hostapi.ReasonJoinTokenInvalid) {
		t.Fatalf("reused join token must be refused, got %v", err)
	}

	h.cp.Revoke(id)
	before := h.cp.Refusals(hostapi.ReasonHostRevoked)
	if err := h.agent.heartbeatOnce(context.Background()); err == nil {
		t.Fatal("a revoked host's heartbeat must be refused")
	}
	h.waitFor(3*time.Second, "revoked state", h.agent.Revoked)
	if h.cp.Refusals(hostapi.ReasonHostRevoked) <= before {
		t.Fatal("fake control plane did not record the host_revoked refusal")
	}
	accepted := h.cp.HeartbeatCount(id)
	time.Sleep(400 * time.Millisecond)
	if got := h.cp.HeartbeatCount(id); got != accepted {
		t.Fatalf("heartbeats were accepted after revocation: %d → %d", accepted, got)
	}
	if err := h.agent.Renew(context.Background()); err == nil {
		t.Fatal("renewal of a revoked host must be refused")
	}
	// A revoked host admits no new VM.
	h.cp.SetDesired(id, replicas(1, nil))
	ds := replicas(1, nil)
	h.agent.applyDesired(context.Background(), &ds)
	h.agent.Reconcile(context.Background())
	if v := h.agent.VMs(); len(v) != 1 || v[0].State != hostapi.StateRefused || v[0].Reason != "host_revoked" {
		t.Fatalf("revoked host must refuse new VMs: %+v", v)
	}
	h.crash()
}

// Renewal at 2/3 of a short lifetime swaps the mTLS certificate without a
// restart, and the control plane pins the new fingerprint.
func TestCertificateRenewsAtTwoThirds(t *testing.T) {
	h := newHarness(t)
	h.cp.CertTTL = 3 * time.Second
	h.cp.AddJoinToken("j")
	h.start(h.config("j"))
	first := h.agent.creds.Load().ClientCert
	h.waitFor(6*time.Second, "renewal", func() bool {
		return h.agent.creds.Load().ClientCert.SerialNumber.Cmp(first.SerialNumber) != 0
	})
	// Renewed at two thirds of the lifetime, not before.
	if since := time.Since(first.NotBefore); since < 1800*time.Millisecond {
		t.Fatalf("renewed too early: %s after issue (want ≈ 2/3 of 3 s)", since)
	}
	id := h.agent.HostID()
	n := h.cp.HeartbeatCount(id)
	h.waitFor(3*time.Second, "heartbeats on the renewed cert", func() bool { return h.cp.HeartbeatCount(id) > n+2 })
	// The renewed key files are 0600.
	for _, f := range []string{"client.key", "svid-ca.key"} {
		info, err := os.Stat(filepath.Join(h.state, "pki", f))
		if err != nil || info.Mode().Perm() != 0o600 {
			t.Fatalf("%s must be 0600: %v %v", f, info.Mode(), err)
		}
	}
	h.crash()
}

// The pinned CA is the only trust root: a control plane signed by another CA
// is refused at the TLS layer, before any token is sent.
func TestPinnedCARefusesAnotherControlPlane(t *testing.T) {
	h := newHarness(t)
	other, _ := fakecp.New()
	cfg := h.config("j")
	cfg.PinnedCA = []byte(other.CA.PEM)
	h.cp.AddJoinToken("j")
	a, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	err = a.Enrol(context.Background())
	if err == nil || !strings.Contains(err.Error(), "certificate") {
		t.Fatalf("enrol against an unpinned CA must fail on the certificate, got %v", err)
	}
	if h.cp.Refusals(hostapi.ReasonJoinTokenInvalid) != 0 {
		t.Fatal("the join token reached a control plane outside the pin")
	}
}

// P8a-T2 (in-process): replicas: 3 converges; a crash plus a deleted bbolt
// file converges again within 10 s with no duplicate VMs and the running VMs
// untouched (same PIDs).
func TestP8aT2ConvergeAfterCrashAndCacheLoss(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j")
	h.start(h.config("j"))
	id := h.agent.HostID()
	h.cp.SetDesired(id, replicas(3, nil))
	h.waitFor(10*time.Second, "3 serving replicas", func() bool { return len(h.serving()) == 3 })
	before := pids(t, h.mgr)
	if len(before) != 3 {
		t.Fatalf("want 3 VM processes, got %v", before)
	}

	h.crash()
	if err := os.Remove(filepath.Join(h.state, "cache.db")); err != nil {
		t.Fatal(err)
	}
	started := time.Now()
	h.start(h.config(""))
	if h.agent.HostID() != id {
		t.Fatalf("restart must keep the host identity: %s != %s", h.agent.HostID(), id)
	}
	h.waitFor(10*time.Second, "reconverged replicas", func() bool { return len(h.serving()) == 3 })
	took := time.Since(started)
	after := pids(t, h.mgr)
	if len(after) != 3 {
		t.Fatalf("duplicate or missing VMs after reconverge: %v", after)
	}
	for vm, pid := range before {
		if after[vm] != pid {
			t.Fatalf("VM %s was restarted (pid %d → %d): running VMs must be untouched", vm, pid, after[vm])
		}
	}
	t.Logf("reconverged in %s with the same 3 PIDs", took)

	// Scale in: the extras are stopped, nothing else is touched.
	h.cp.SetDesired(id, replicas(1, nil))
	h.waitFor(10*time.Second, "scale to 1", func() bool { return len(pids(t, h.mgr)) == 1 && len(h.serving()) == 1 })
	if pids(t, h.mgr)["vm_1"] != before["vm_1"] {
		t.Fatal("scale-in restarted the surviving VM")
	}
	h.crash()
}

func pids(t *testing.T, m vmm.Manager) map[string]int {
	t.Helper()
	vms, err := m.Scan()
	if err != nil {
		t.Fatal(err)
	}
	out := map[string]int{}
	for _, v := range vms {
		if _, dup := out[v.ID]; dup {
			out[v.ID+"#dup"] = v.PID
			continue
		}
		out[v.ID] = v.PID
	}
	return out
}

// Watchdog: a VM process exit is noticed at once and the VM is restarted; the
// 4th failure inside the window is a crashloop, reported to the control plane.
func TestWatchdogRestartsThenCrashloops(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j")
	h.start(h.config("j"))
	id := h.agent.HostID()
	h.cp.SetDesired(id, replicas(1, nil))
	h.waitFor(10*time.Second, "serving", func() bool { return len(h.serving()) == 1 })
	for i := 1; i <= 4; i++ {
		p := pids(t, h.mgr)["vm_1"]
		if p == 0 {
			t.Fatalf("round %d: no VM process", i)
		}
		killPID(p)
		if i < 4 {
			h.waitFor(10*time.Second, fmt.Sprintf("restart %d", i), func() bool {
				s := h.serving()["vm_1"]
				return s.Restarts == i && pids(t, h.mgr)["vm_1"] != p && pids(t, h.mgr)["vm_1"] != 0
			})
		}
	}
	h.waitFor(10*time.Second, "crashloop", func() bool {
		for _, v := range h.agent.VMs() {
			if v.VMID == "vm_1" && v.State == hostapi.StateCrashloop {
				return true
			}
		}
		return false
	})
	h.waitFor(5*time.Second, "crashloop event at the control plane", func() bool {
		for _, e := range h.cp.Events() {
			if e.Kind == "crashloop" && e.VMID == "vm_1" {
				return true
			}
		}
		return false
	})
	if len(pids(t, h.mgr)) != 0 {
		t.Fatal("a crashlooping VM must be stopped, not left running")
	}
	h.crash()
}

// A hung guest (alive, not answering pings) is unhealthy after 3 misses.
func TestWatchdogMissedPings(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j")
	h.start(h.config("j"))
	id := h.agent.HostID()
	h.cp.SetDesired(id, replicas(1, nil))
	h.waitFor(10*time.Second, "serving", func() bool { return len(h.serving()) == 1 })
	p := pids(t, h.mgr)["vm_1"]
	stopPID(p) // SIGSTOP: the process exists but answers nothing
	stopped := time.Now()
	h.waitFor(5*time.Second, "restart after missed pings", func() bool {
		s := h.serving()["vm_1"]
		return s.Restarts == 1
	})
	// 3 misses at a 100 ms tick, each ping bounded inside its tick: the
	// unhealthy transition lands within 3 ticks (+ scheduling slack).
	var at time.Time
	h.waitFor(5*time.Second, "missed-ping transition reported", func() bool {
		for _, e := range h.cp.Events() {
			if e.Kind == "vm_state" && e.To == hostapi.StateUnhealthy && strings.HasPrefix(e.Detail["reason"], "missed_pings") {
				at, _ = time.Parse(time.RFC3339Nano, e.At)
				return true
			}
		}
		return false
	})
	if took := at.Sub(stopped); took > 600*time.Millisecond {
		t.Fatalf("hung VM declared unhealthy after %s (want ≤ 3 × 100 ms ticks + slack)", took)
	}
	killPID(p)
	h.crash()
}

// per_run stays OFF by default: the claims-frozen switch refuses it with a
// named reason, and turning the switch off admits it.
func TestPerRunGatedByClaimsFrozen(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j")
	h.start(h.config("j"))
	id := h.agent.HostID()
	ds := hostapi.DesiredState{VMs: []hostapi.DesiredVM{{ReplicaID: "rep_r1", VMID: "run-1", DeploymentID: "coder",
		Mode: hostapi.ModePerRun, State: hostapi.DesiredRunning,
		Runs: []hostapi.RunBind{{RunID: "run_1", Epoch: 1, Handles: hostapi.Handles{Run: "boss_run_h1"}}}}}}
	h.cp.SetDesired(id, ds)
	h.waitFor(5*time.Second, "refused per_run", func() bool {
		for _, v := range h.agent.VMs() {
			if v.VMID == "run-1" && v.State == hostapi.StateRefused && v.Reason == "microvm_claims_frozen" {
				return true
			}
		}
		return false
	})
	h.crash()
	cfg := h.config("")
	cfg.ClaimsFrozen = false
	h.start(cfg)
	h.waitFor(10*time.Second, "per_run serving once unfrozen", func() bool { return len(h.serving()) == 1 })
	h.crash()
}

// The guest gateway end to end on the process backend: guest loopback port →
// boss-vsockd relay → per-VM Unix socket → gateway → the control plane's run
// plane (relative endpoint). The guest's Authorization, Cookie and forged
// X-Boss-* never survive; the control plane authenticates the HOST (BossHost
// proof) and learns the replica from X-Boss-Replica, which the gateway sets
// from the socket; lease headers pass through. A guest_calls row reaches the
// control plane in its schema.
func TestGatewayEndToEndThroughRelay(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j")
	seen := &seenLog{}
	h.cp.RunPlane = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen.mu.Lock()
		seen.reqs = append(seen.reqs, seenReq{auth: r.Header.Get("Authorization"), cookie: r.Header.Get("Cookie"),
			forgedRunID: r.Header.Get("X-Boss-Run-Id"), assertion: r.Header.Get("Boss-Run-Assertion"),
			replica: r.Header.Get("X-Boss-Replica"), job: r.Header.Get("X-Boss-Job"), host: r.Header.Get("X-Fake-Host"),
			path: r.URL.Path})
		seen.mu.Unlock()
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true})
	})
	h.start(h.config("j"))
	id := h.agent.HostID()
	h.cp.SetDesired(id, replicas(2, nil)) // endpoints default: run_plane = "/v1/run-plane"
	h.waitFor(10*time.Second, "2 serving", func() bool { return len(h.serving()) == 2 })

	resp, err := h.agent.Control("vm_1", "selfcheck_gateway")
	if err != nil || !resp.OK {
		t.Fatalf("selfcheck_gateway: %v %+v", err, resp)
	}
	if resp.Selfcheck["status"] != "200" {
		t.Fatalf("relay → gateway → run plane failed: %+v\n%s", resp.Selfcheck, h.logs.String())
	}
	got := seen.last()
	if got.host != id || !strings.HasPrefix(got.auth, "BossHost ") {
		t.Fatalf("the run plane must authenticate the host by its proof: %+v", got)
	}
	if got.cookie != "" || got.forgedRunID != "" || strings.Contains(got.auth, "guest-supplied") {
		t.Fatalf("guest headers survived: %+v", got)
	}
	if got.replica != "rep_1" || got.job != "job_selfcheck" || got.path != "/v1/run-plane/selfcheck" || got.assertion == "" {
		t.Fatalf("replica identity / lease / path wrong: %+v", got)
	}
	h.waitFor(5*time.Second, "guest_calls row", func() bool { return len(h.cp.GuestCalls()) > 0 })
	row := h.cp.GuestCalls()[0]
	if row.VMID != "vm_1" || row.Route != "run_plane" || row.Verdict != "allow" || row.Status != 200 || row.CallID == "" {
		t.Fatalf("guest_calls row: %+v", row)
	}
	// State changes reached /observed with replica ids (not just heartbeats).
	h.waitFor(5*time.Second, "observed push", func() bool {
		for _, r := range h.cp.Observed(id) {
			if r.ReplicaID == "rep_1" && r.State == hostapi.StateServing {
				return true
			}
		}
		return false
	})
	h.crash()
}

// A control plane without /events still gets every state change through
// heartbeat and observed; the agent stops posting events.
func TestNoEventsEndpointIsTolerated(t *testing.T) {
	h := newHarness(t)
	h.cp.NoEvents = true
	h.cp.AddJoinToken("j")
	h.start(h.config("j"))
	h.cp.SetDesired(h.agent.HostID(), replicas(1, nil))
	h.waitFor(10*time.Second, "serving", func() bool { return len(h.serving()) == 1 })
	h.waitFor(5*time.Second, "events disabled", h.agent.noEvents.Load)
	h.crash()
}

type seenReq struct {
	auth, cookie, forgedRunID, assertion, peerURI, replica, job, host, path string
}

type seenLog struct {
	mu   sync.Mutex
	reqs []seenReq
}

func (s *seenLog) last() seenReq {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.reqs) == 0 {
		return seenReq{}
	}
	return s.reqs[len(s.reqs)-1]
}
