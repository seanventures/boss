package hostd

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
)

func (h *harness) inboxRuns(vmID string) []string {
	r, err := h.agent.Control(vmID, "runs")
	if err != nil || r == nil {
		return nil
	}
	return r.Runs
}

func (h *harness) totalInboxRuns(n int) int {
	total := 0
	for i := 1; i <= n; i++ {
		total += len(h.inboxRuns(fmt.Sprintf("vm_%d", i)))
	}
	return total
}

// P8a-T6: 50 replicas on one host make exactly ONE outstanding hosts/next.
// The poll body is {wait_ms, state_gen} only; in steady state there is no
// resync; claimed runs reach the VMs' inboxes.
func TestP8aT6OneOutstandingWorkPollFor50Replicas(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j")
	cfg := h.config("j")
	cfg.MaxVMs = 60
	h.start(cfg)
	id := h.agent.HostID()
	h.cp.SetDesired(id, replicas(50, nil))
	h.waitFor(60*time.Second, "50 serving replicas", func() bool { return len(h.serving()) == 50 })
	h.waitFor(10*time.Second, "BOSS knows all 50 replicas", func() bool { return h.cp.Work(id).ReplicasKnown == 50 })

	// Steady state: polls keep cycling (1 s wait), no resync, one in flight.
	time.Sleep(1500 * time.Millisecond)
	before := h.cp.Work(id)
	for i := 0; i < 40; i++ {
		h.cp.Enqueue("dep-x", fmt.Sprintf("run_t6_%d", i))
	}
	h.waitFor(10*time.Second, "40 runs delivered", func() bool { return h.totalInboxRuns(50) == 40 })
	time.Sleep(2500 * time.Millisecond)
	after := h.cp.Work(id)
	if after.MaxInflight != 1 {
		t.Fatalf("max outstanding hosts/next = %d, want exactly 1", after.MaxInflight)
	}
	if after.NextCalls < before.NextCalls+2 {
		t.Fatalf("the poll is not re-issued: %d → %d calls", before.NextCalls, after.NextCalls)
	}
	if strings.Join(after.BodyKeys, ",") != "state_gen,wait_ms" {
		t.Fatalf("poll body carries %v; it must be only {wait_ms, state_gen}", after.BodyKeys)
	}
	if after.Resyncs != before.Resyncs {
		t.Fatalf("steady state resynced (%d → %d)", before.Resyncs, after.Resyncs)
	}
	if len(after.HandBacks) != 0 {
		t.Fatalf("unexpected hand-backs: %v", after.HandBacks)
	}
	if n := h.agent.gw.Runs.Len(); n != 40 {
		t.Fatalf("the host must hold the 40 run tokens (never the VMs): %d", n)
	}
	t.Logf("50 replicas: %d polls, max in flight %d, %d resyncs (all before steady state), 40/40 runs delivered",
		after.NextCalls, after.MaxInflight, after.Resyncs)
	h.crash()
}

// P8a-T7: a run assigned to a replica that dies before accepting is handed
// back and claimed by another replica within 3 s.
func TestP8aT7DeadReplicaHandsBackWithin3s(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j")
	h.start(h.config("j"))
	id := h.agent.HostID()
	h.cp.SetDesired(id, replicas(3, nil))
	h.waitFor(15*time.Second, "3 serving", func() bool { return len(h.serving()) == 3 })
	h.waitFor(10*time.Second, "BOSS knows the replicas", func() bool { return h.cp.Work(id).ReplicasKnown == 3 })
	victim := pids(t, h.mgr)["vm_1"]
	killed := false
	h.cp.OnClaim = func(a hostapi.Assignment) {
		if a.RunID == "run_t7" && !killed { // dies after the claim, before accepting
			killed = true
			killPID(victim)
			// ...and is not coming back (scaled in), so the re-claim must
			// land on another replica, not on a restarted rep_1.
			ds := replicas(3, nil)
			ds.VMs = ds.VMs[1:]
			go h.cp.SetDesired(id, ds)
			time.Sleep(50 * time.Millisecond)
		}
	}
	h.cp.PreferReplica("rep_1")
	start := time.Now()
	h.cp.Enqueue("dep-x", "run_t7")
	var by string
	defer func() {
		if t.Failed() {
			t.Logf("work stats: %+v", h.cp.Work(id))
		}
	}()
	h.waitFor(3*time.Second, "re-claim by another replica", func() bool {
		for _, c := range h.cp.Work(id).Claims {
			if c.RunID == "run_t7" && c.ReplicaID != "rep_1" {
				by = c.ReplicaID
				return true
			}
		}
		return false
	})
	took := time.Since(start)
	w := h.cp.Work(id)
	if len(w.HandBacks) > 3 {
		t.Fatalf("hand-back hot loop: %d hand-backs", len(w.HandBacks))
	}
	if len(w.HandBacks) == 0 || w.HandBacks[0] != "run_t7" || !killed {
		t.Fatalf("no hand-back recorded: %+v", w.HandBacks)
	}
	vm := strings.Replace(by, "rep_", "vm_", 1)
	h.waitFor(3*time.Second, "accepted by the new replica", func() bool {
		return strings.Contains(strings.Join(h.inboxRuns(vm), ","), "run_t7")
	})
	if w.Claims[0].Epoch >= w.Claims[len(w.Claims)-1].Epoch {
		t.Fatal("the re-claim must carry a higher epoch")
	}
	t.Logf("run_t7: rep_1 died after the claim, handed back, re-claimed by %s in %s", by, took)
	h.crash()
}

// A VM with no free slot (by the host's own 500 ms ping) refuses delivery and
// the run is handed back at once.
func TestHandBackOnRefusal(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j")
	cfg := h.config("j")
	cfg.RunHoldMS = 10000 // the stand-in inbox keeps the slot busy
	h.start(cfg)
	id := h.agent.HostID()
	ds := replicas(1, nil)
	ds.VMs[0].Slots = 1
	h.cp.SetDesired(id, ds)
	h.waitFor(15*time.Second, "serving", func() bool { return len(h.serving()) == 1 })
	h.waitFor(10*time.Second, "BOSS knows the replica", func() bool { return h.cp.Work(id).ReplicasKnown == 1 })
	h.cp.SetSlotsOverride(3) // BOSS's count drifts: it thinks 3 slots are free
	for i := 0; i < 3; i++ {
		h.cp.Enqueue("dep-x", fmt.Sprintf("run_r%d", i))
	}
	h.waitFor(5*time.Second, "two hand-backs", func() bool { return len(h.cp.Work(id).HandBacks) >= 2 })
	h.cp.SetSlotsOverride(1) // BOSS recounts
	if got := h.inboxRuns("vm_1"); len(got) != 1 {
		t.Fatalf("the VM accepted %v; want exactly one run (1 slot)", got)
	}
	h.crash()
}

// A generation mismatch answers {resync:true}; the host PUTs its full map
// once and resumes polling.
func TestResyncOnGenerationMismatch(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j")
	h.start(h.config("j"))
	id := h.agent.HostID()
	h.cp.SetDesired(id, replicas(2, nil))
	h.waitFor(15*time.Second, "serving", func() bool { return len(h.serving()) == 2 })
	// /observed already carries state_gen, so a resync is not normally needed.
	h.waitFor(10*time.Second, "BOSS knows the replicas", func() bool { return h.cp.Work(id).ReplicasKnown == 2 })
	time.Sleep(1500 * time.Millisecond)
	puts := h.cp.Work(id).Puts
	h.cp.ForgetGen(id)
	h.waitFor(5*time.Second, "resync after mismatch", func() bool { return h.cp.Work(id).Puts == puts+1 })
	h.cp.Enqueue("dep-x", "run_after_resync")
	h.waitFor(5*time.Second, "work flows after the resync", func() bool { return h.totalInboxRuns(2) == 1 })
	h.crash()
}

// A crashloop's single replacement attempt that fails again is quarantined:
// the restart history survives the crashloop retry.
func TestCrashloopThenQuarantine(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j")
	cfg := h.config("j")
	cfg.RestartWindow = 3 * time.Second
	h.start(cfg)
	id := h.agent.HostID()
	h.cp.SetDesired(id, replicas(1, nil))
	h.waitFor(10*time.Second, "serving", func() bool { return len(h.serving()) == 1 })
	state := func() string {
		for _, v := range h.agent.VMs() {
			if v.VMID == "vm_1" {
				return v.State
			}
		}
		return ""
	}
	for i := 0; i < 4 && state() != hostapi.StateCrashloop; i++ {
		p := pids(t, h.mgr)["vm_1"]
		killPID(p)
		h.waitFor(10*time.Second, "next state", func() bool {
			np := pids(t, h.mgr)["vm_1"]
			return state() == hostapi.StateCrashloop || (np != 0 && np != p && len(h.serving()) == 1)
		})
	}
	h.waitFor(5*time.Second, "crashloop", func() bool { return state() == hostapi.StateCrashloop })
	h.waitFor(10*time.Second, "one replacement after the window", func() bool { return len(h.serving()) == 1 })
	killPID(pids(t, h.mgr)["vm_1"])
	h.waitFor(5*time.Second, "quarantined", func() bool { return state() == hostapi.StateQuarantined })
	time.Sleep(4 * time.Second) // past another window: still nothing restarts it
	if state() != hostapi.StateQuarantined || len(pids(t, h.mgr)) != 0 {
		t.Fatalf("quarantine did not hold: %s %v", state(), pids(t, h.mgr))
	}
	// A spec change clears it.
	ds := replicas(1, nil)
	ds.VMs[0].ImageDigest = "sha256:fixed"
	h.cp.SetDesired(id, ds)
	h.waitFor(10*time.Second, "fresh VM after a spec change", func() bool { return len(h.serving()) == 1 })
	h.crash()
}

// After BOSS_HOSTD_HEADLESS_MAX without the control plane the host stops
// serving: the gateway refuses.
func TestHeadlessCapStopsServing(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j")
	cfg := h.config("j")
	cfg.HeadlessMax = 1500 * time.Millisecond
	h.start(cfg)
	id := h.agent.HostID()
	h.cp.SetDesired(id, replicas(1, nil))
	h.waitFor(10*time.Second, "serving", func() bool { return len(h.serving()) == 1 })
	if r, _ := h.agent.Control("vm_1", "selfcheck_gateway"); r == nil || r.Selfcheck["status"] == "503" {
		t.Fatalf("gateway refused while connected: %+v", r)
	}
	h.cp.Close()
	h.waitFor(5*time.Second, "headless cap", h.agent.headlessExpired)
	r, _ := h.agent.Control("vm_1", "selfcheck_gateway")
	if r == nil || r.Selfcheck["status"] != "503" || !strings.Contains(r.Selfcheck["body"], "host_not_serving") {
		t.Fatalf("gateway must refuse after the headless cap: %+v", r)
	}
	if len(pids(t, h.mgr)) != 1 {
		t.Fatal("the VM itself keeps running (only serving stops)")
	}
	h.crash()
}

// The spent join token file is deleted after enrolment.
func TestJoinTokenFileDeletedAfterEnrol(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j-file")
	f := filepath.Join(h.root, "join")
	if err := os.WriteFile(f, []byte("j-file\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg := h.config("j-file")
	cfg.JoinTokenFile = f
	h.start(cfg)
	if _, err := os.Stat(f); !os.IsNotExist(err) {
		t.Fatalf("join token file still present: %v", err)
	}
	h.crash()
}

// P8a-T2 variant: after a crash and bbolt loss, an adopted VM whose recorded
// spec differs from desired (here the image digest) is replaced; the others
// are untouched.
func TestAdoptedVMWithChangedDigestIsReplaced(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j")
	h.start(h.config("j"))
	id := h.agent.HostID()
	h.cp.SetDesired(id, replicas(3, nil))
	h.waitFor(10*time.Second, "3 serving", func() bool { return len(h.serving()) == 3 })
	before := pids(t, h.mgr)
	h.crash()
	_ = os.Remove(filepath.Join(h.state, "cache.db"))
	ds := replicas(3, nil)
	ds.VMs[1].ImageDigest = "sha256:new" // vm_2 changed while hostd was down
	h.cp.SetDesired(id, ds)
	h.start(h.config(""))
	h.waitFor(10*time.Second, "reconverged", func() bool {
		p := pids(t, h.mgr)
		return len(h.serving()) == 3 && len(p) == 3 && p["vm_2"] != before["vm_2"]
	})
	after := pids(t, h.mgr)
	if after["vm_1"] != before["vm_1"] || after["vm_3"] != before["vm_3"] {
		t.Fatalf("matching VMs must be untouched: %v → %v", before, after)
	}
	h.waitFor(5*time.Second, "adopted_mismatch event", func() bool {
		for _, e := range h.cp.Events() {
			if e.Kind == "adopted_mismatch" && e.VMID == "vm_2" && e.Detail["field"] == "image_digest" {
				return true
			}
		}
		return false
	})
	h.crash()
}

// Review MED (PR #121): the run's tokens are minted BEFORE the VM hears of
// the run; a refused mint hands the run back with the reason and the VM never
// accepts it (P8b S13).
func TestRunTokensAreMintedBeforeDeliveryOrTheRunIsHandedBack(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j")
	h.start(h.config("j"))
	id := h.agent.HostID()
	h.cp.SetDesired(id, replicas(1, nil))
	h.waitFor(15*time.Second, "serving", func() bool { return len(h.serving()) == 1 })
	h.waitFor(10*time.Second, "BOSS knows the replica", func() bool { return h.cp.Work(id).ReplicasKnown == 1 })

	h.cp.SetTokensRefused(true)
	h.cp.Enqueue("dep-x", "run_tok_refused")
	h.waitFor(5*time.Second, "a hand-back", func() bool { return len(h.cp.Work(id).HandBacks) >= 1 })
	if got := h.inboxRuns("vm_1"); len(got) != 0 {
		t.Fatalf("the VM accepted %v without its tokens", got)
	}
	// (BOSS re-queues it and the host may already be re-claiming it: what
	// matters is that no usable token exists and the VM never got the run.)
	if _, ok := h.agent.gw.Keys.Pair("run_tok_refused", hostapi.AudMCP); ok {
		t.Fatal("a refused mint leaves no usable token")
	}

	h.cp.SetTokensRefused(false)
	h.waitFor(5*time.Second, "delivered", func() bool { return len(h.inboxRuns("vm_1")) == 1 })
	if _, ok := h.agent.gw.Keys.Pair("run_tok_refused", hostapi.AudMCP); !ok {
		t.Fatal("a delivered run holds its tokens")
	}
	h.crash()
}
