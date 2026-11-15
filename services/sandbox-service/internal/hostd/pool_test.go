package hostd

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanheiney/boss/services/sandbox-service/internal/vmm"
)

func perRunPool(dep string, minWarm int) hostapi.DesiredState {
	return hostapi.DesiredState{Endpoints: map[string]string{"run_plane": "/v1/run-plane"},
		Pools: []hostapi.Pool{{DeploymentID: dep, ImageDigest: "sha256:coder", MinWarm: minWarm,
			Resources: hostapi.Resources{"vcpu": 1.0, "mem_mib": 128.0}}}}
}

func perRunVM(id, dep string) hostapi.DesiredVM {
	return hostapi.DesiredVM{ReplicaID: "rep_" + id, VMID: "vm_" + id, DeploymentID: dep, Mode: hostapi.ModePerRun,
		State: hostapi.DesiredRunning, ImageDigest: "sha256:coder", Resources: hostapi.Resources{"vcpu": 1.0, "mem_mib": 128.0},
		Runs: []hostapi.RunBind{{RunID: "run_" + id, Epoch: 1, Handles: hostapi.Handles{Run: "boss_run_" + id, Model: "boss_mdl_" + id}}}}
}

func (h *harness) events(kind string) []hostapi.HostEvent {
	var out []hostapi.HostEvent
	for _, e := range h.cp.Events() {
		if e.Kind == kind {
			out = append(out, e)
		}
	}
	return out
}

// LLD §3.4: a per_run pool keeps min_warm VMs warm (restored, no identity).
// The first fill cold-boots and snapshots the shape (clean attested); later
// fills restore. A desired per_run VM is a warm claim: it takes a warm VM
// (known by its slot id), bootstraps it, and the pool refills.
func TestWarmPoolFillsThenClaimIsWarm(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j")
	cfg := h.config("j")
	cfg.ClaimsFrozen = false
	h.start(cfg)
	id := h.agent.HostID()
	h.cp.SetDesired(id, perRunPool("coder", 2))
	h.waitFor(15*time.Second, "2 warm VMs", func() bool { return h.agent.WarmDepth("coder") == 2 })
	h.waitFor(5*time.Second, "heartbeat warm_pools", func() bool {
		hb, _ := h.cp.LastHeartbeat(id)
		return hb != nil && hb.WarmPools["coder"].Depth == 2 && hb.WarmPools["coder"].MinWarm == 2 && hb.Capacity.UsedVMs == 2
	})
	h.waitFor(5*time.Second, "snapshot and warm_ready events", func() bool {
		return len(h.events("snapshot")) == 1 && len(h.events("warm_ready")) == 2
	})
	snaps := h.events("snapshot")
	if snaps[0].Detail["clean_attested"] != "true" || snaps[0].Detail["image"] != "sha256:coder" {
		t.Fatalf("one clean snapshot of the shape expected: %+v", snaps)
	}
	if h.agent.cfg.Manager.Snapshots()[0].Shape.Mode != hostapi.ModePerRun {
		t.Fatal("snapshot shape must carry the mode")
	}
	ready := h.events("warm_ready")
	if ready[0].Detail["restored"] == ready[1].Detail["restored"] {
		t.Fatalf("first fill cold boots, second restores: %+v", ready)
	}

	ds := perRunPool("coder", 2)
	ds.VMs = append(ds.VMs, perRunVM("01", "coder"))
	claimed := time.Now()
	h.cp.SetDesired(id, ds)
	h.waitFor(10*time.Second, "claimed VM serving", func() bool { _, ok := h.serving()["vm_01"]; return ok })
	h.agent.mu.Lock()
	st := h.agent.vms["vm_01"]
	h.agent.mu.Unlock()
	st.mu.Lock()
	vm := st.vm
	st.mu.Unlock()
	if !strings.HasPrefix(vm.ID, "w-") || vm.Spec.VMID != "vm_01" || vm.Spec.Warm {
		t.Fatalf("the claim must reuse a warm VM under its slot id with the claimed spec: %+v", vm.Spec)
	}
	if tainted, why := vm.Tainted(); !tainted || why != "identity" {
		t.Fatalf("a bootstrapped VM must be tainted: %v %q", tainted, why)
	}
	if _, err := h.agent.cfg.Manager.Snapshot(context.Background(), vm); !errors.Is(err, vmm.ErrTainted) {
		t.Fatalf("snapshot of a VM with an identity must be refused: %v", err)
	}
	resp, err := h.agent.Control("vm_01", "ping")
	if err != nil || resp.VMID != "vm_01" || !resp.Bootstrapped {
		t.Fatalf("guest identity after the claim: %v %+v", err, resp)
	}
	h.waitFor(10*time.Second, "pool refilled", func() bool { return h.agent.WarmDepth("coder") == 2 })
	h.waitFor(5*time.Second, "claim timing event", func() bool {
		for _, e := range h.events("vm_timing") {
			if e.VMID == "vm_01" && (e.Detail["kind"] == "warm_claim" || e.Detail["kind"] == "cold_claim") {
				return true
			}
		}
		return false
	})
	if time.Since(claimed) > 10*time.Second {
		t.Fatal("warm claim took too long")
	}
}

// P8b-T4 (process backend): claimed clones report distinct vm_ids and RNG
// output, carry no token, and the restored ones say so.
func TestClaimedClonesAreDistinct(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j")
	cfg := h.config("j")
	cfg.ClaimsFrozen = false
	h.start(cfg)
	id := h.agent.HostID()
	ds := perRunPool("coder", 3)
	h.cp.SetDesired(id, ds)
	h.waitFor(15*time.Second, "3 warm", func() bool { return h.agent.WarmDepth("coder") == 3 })
	for i := 1; i <= 3; i++ {
		ds.VMs = append(ds.VMs, perRunVM("0"+string(rune('0'+i)), "coder"))
	}
	h.cp.SetDesired(id, ds)
	h.waitFor(15*time.Second, "3 claimed VMs serving", func() bool { return len(h.serving()) == 3 })
	seen := map[string]bool{}
	restored := 0
	for _, v := range []string{"vm_01", "vm_02", "vm_03"} {
		r, err := h.agent.Control(v, "rng")
		if err != nil || r.VMID != v || len(r.RNG) != 32 {
			t.Fatalf("%s rng: %v %+v", v, err, r)
		}
		if seen[r.RNG] {
			t.Fatalf("two clones share RNG output %s", r.RNG)
		}
		seen[r.RNG] = true
		if r.Restored {
			restored++
		}
	}
	if restored < 2 {
		t.Fatalf("at least two of three clones must come from the snapshot, got %d", restored)
	}
	// No token anywhere near the guest: the bootstrap carried opaque handles
	// only, and the host's run tokens never left its memory.
	for _, e := range h.cp.Events() {
		for _, v := range e.Detail {
			if strings.Contains(v, "lease_") {
				t.Fatalf("a token leaked into an event: %+v", e)
			}
		}
	}
}

// LLD §3.17.4 / operator decision: a frozen host fills no per_run pool, and
// still reports the pool (depth 0) so the control plane sees why.
func TestFrozenHostKeepsPoolEmpty(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j")
	h.start(h.config("j")) // ClaimsFrozen: true
	id := h.agent.HostID()
	h.cp.SetDesired(id, perRunPool("coder", 2))
	h.waitFor(5*time.Second, "heartbeat with the pool", func() bool {
		hb, _ := h.cp.LastHeartbeat(id)
		return hb != nil && hb.ClaimsFrozen && hb.WarmPools["coder"].MinWarm == 2
	})
	time.Sleep(500 * time.Millisecond)
	if h.agent.WarmDepth("coder") != 0 || len(h.events("warm_ready")) != 0 {
		t.Fatal("a frozen host must fill no per_run pool")
	}
}

// Replicas: the first cold boot of a shape snapshots it before any identity;
// a replica shape keeps one warm spare (surge); a snapshot nobody references
// is garbage-collected after the grace.
func TestReplicaSpareAndSnapshotGC(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j")
	cfg := h.config("j")
	cfg.SnapshotGCGrace, cfg.SnapshotGCEvery, cfg.ReplicaSurge = 200*time.Millisecond, 100*time.Millisecond, 1
	h.start(cfg)
	id := h.agent.HostID()
	ds := replicas(1, map[string]string{"run_plane": "/v1/run-plane"})
	h.cp.SetDesired(id, ds)
	h.waitFor(10*time.Second, "replica serving", func() bool { _, ok := h.serving()["vm_1"]; return ok })
	h.waitFor(10*time.Second, "one replica spare", func() bool { return h.agent.WarmDepth("dep-x") == 1 })
	snaps := h.agent.cfg.Manager.Snapshots()
	if len(snaps) != 1 || snaps[0].Shape.ImageDigest != "sha256:agent" || snaps[0].Shape.Mode != hostapi.ModeReplica {
		t.Fatalf("replica shape snapshot: %+v", snaps)
	}
	// Roll the image: the old snapshot loses every reference and is collected;
	// the spare of the old shape is dropped.
	ds.VMs[0].ImageDigest = "sha256:agent-v2"
	h.cp.SetDesired(id, ds)
	h.waitFor(10*time.Second, "old snapshot collected", func() bool {
		for _, s := range h.agent.cfg.Manager.Snapshots() {
			if s.Shape.ImageDigest == "sha256:agent" {
				return false
			}
		}
		return len(h.events("snapshot_gc")) == 1
	})
	h.waitFor(10*time.Second, "new shape serving with its spare", func() bool {
		v, ok := h.serving()["vm_1"]
		return ok && v.ImageDigest == "sha256:agent-v2" && h.agent.WarmDepth("dep-x") == 1
	})
	for _, e := range h.events("warm_dropped") {
		if e.Detail["why"] == "" {
			t.Fatal("dropped spare without a reason")
		}
	}
}

// A hostd crash leaves warm VMs running; the restarted host adopts them back
// into the pool instead of booting duplicates.
func TestWarmVMsAdoptedAfterCrash(t *testing.T) {
	h := newHarness(t)
	h.cp.AddJoinToken("j")
	cfg := h.config("j")
	cfg.ClaimsFrozen = false
	h.start(cfg)
	id := h.agent.HostID()
	h.cp.SetDesired(id, perRunPool("coder", 2))
	h.waitFor(15*time.Second, "2 warm", func() bool { return h.agent.WarmDepth("coder") == 2 })
	before, _ := h.mgr.Scan()
	h.crash()
	cfg2 := h.config("")
	cfg2.ClaimsFrozen = false
	h.start(cfg2)
	h.waitFor(10*time.Second, "pool adopted", func() bool { return h.agent.WarmDepth("coder") == 2 })
	time.Sleep(300 * time.Millisecond)
	after, _ := h.mgr.Scan()
	if len(before) != 2 || len(after) != 2 {
		t.Fatalf("warm VMs before %d after %d: duplicates or losses", len(before), len(after))
	}
}
