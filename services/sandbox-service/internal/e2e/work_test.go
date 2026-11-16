package e2e

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/seanventures/boss/services/sandbox-service/internal/guest"
	"github.com/seanventures/boss/services/sandbox-service/internal/hostapi"
)

func (e *env) inboxRuns(id string) []string {
	return e.allInboxRuns()[id]
}

// allInboxRuns asks every VM's inbox stand-in what it accepted (one scan).
func (e *env) allInboxRuns() map[string][]string {
	out := map[string][]string{}
	vms, _ := e.mgr.Scan()
	for _, vm := range vms {
		dl := time.Now().Add(3 * time.Second)
		c, err := e.mgr.DialControl(vm, dl)
		if err != nil {
			continue
		}
		r, err := guest.Exchange(c, guest.Request{Op: "runs"}, dl)
		c.Close()
		if err == nil && r != nil {
			out[vm.ID] = r.Runs
		}
	}
	return out
}

// P8a-T2 variant with the real binary: kill -9 + bbolt delete while vm_02's
// desired image digest changed → vm_02 is replaced, vm_01/vm_03 untouched.
func TestP8aT2ChangedDigestWhileDown(t *testing.T) {
	e := newEnv(t)
	e.startHostd()
	e.waitFor(10*time.Second, "enrolment", func() bool { return e.hostID() != "" })
	e.cp.SetDesired(e.hostID(), desired(3, "10.200.4.0/23", true, nil))
	e.waitFor(60*time.Second, "3 serving", func() bool { return len(e.servingSince(time.Time{})) == 3 })
	before := e.pids()
	e.kill9()
	_ = os.Remove(filepath.Join(e.state, "cache.db"))
	ds := desired(3, "10.200.4.0/23", true, nil)
	ds.VMs[1].ImageDigest = "sha256:p8a-agent-v2"
	e.cp.SetDesired(e.hostID(), ds)
	restart := time.Now()
	e.startHostd()
	took := e.waitFor(20*time.Second, "reconverged with vm_02 replaced", func() bool {
		p := e.pids()
		return len(e.servingSince(restart)) == 3 && len(p) == 3 && len(p["vm_02"]) == 1 && p["vm_02"][0] != before["vm_02"][0]
	})
	after := e.pids()
	if after["vm_01"][0] != before["vm_01"][0] || after["vm_03"][0] != before["vm_03"][0] {
		t.Fatalf("unchanged VMs restarted: %v → %v", before, after)
	}
	t.Logf("backend=%s: vm_02 (digest changed while hostd was down) replaced, vm_01/vm_03 untouched; %s", e.backend, took)
}

// P8a-T6 with the real binary: N replicas (default 50) on one host make
// exactly one outstanding hosts/next, whose body is only {wait_ms, state_gen}.
func TestP8aT6OneOutstandingPoll(t *testing.T) {
	n, _ := strconv.Atoi(envOr("BOSS_E2E_T6_REPLICAS", "50"))
	e := newEnv(t)
	e.startHostd()
	e.waitFor(10*time.Second, "enrolment", func() bool { return e.hostID() != "" })
	ds := desired(n, "10.200.4.0/23", true, nil)
	e.cp.SetDesired(e.hostID(), ds)
	took := e.waitFor(240*time.Second, fmt.Sprintf("%d serving", n), func() bool { return len(e.servingSince(time.Time{})) == n })
	e.waitFor(20*time.Second, "BOSS knows every replica", func() bool { return e.cp.Work(e.hostID()).ReplicasKnown >= n })
	time.Sleep(3 * time.Second)
	before := e.cp.Work(e.hostID())
	for i := 0; i < n; i++ {
		e.cp.Enqueue("agent-x", fmt.Sprintf("run_t6_%d", i))
	}
	e.waitFor(30*time.Second, fmt.Sprintf("%d runs delivered", n), func() bool {
		total := 0
		for _, runs := range e.allInboxRuns() {
			total += len(runs)
		}
		return total == n
	})
	time.Sleep(3 * time.Second)
	w := e.cp.Work(e.hostID())
	if w.MaxInflight != 1 || strings.Join(w.BodyKeys, ",") != "state_gen,wait_ms" || w.Resyncs != before.Resyncs || len(w.HandBacks) != 0 {
		t.Fatalf("work protocol: %+v", w)
	}
	t.Logf("backend=%s: %d replicas up in %s; %d polls, max in flight %d, body %v, %d/%d runs delivered, 0 resyncs in steady state",
		e.backend, n, took, w.NextCalls, w.MaxInflight, w.BodyKeys, n, n)
}

// P8a-T7 with the real binary: the VM a run was claimed for dies before
// accepting; the run is handed back and claimed by another replica ≤ 3 s.
func TestP8aT7HandBackOnDeadVM(t *testing.T) {
	e := newEnv(t)
	e.startHostd()
	e.waitFor(10*time.Second, "enrolment", func() bool { return e.hostID() != "" })
	e.cp.SetDesired(e.hostID(), desired(3, "10.200.4.0/23", true, nil))
	e.waitFor(60*time.Second, "3 serving", func() bool { return len(e.servingSince(time.Time{})) == 3 })
	e.waitFor(20*time.Second, "BOSS knows the replicas", func() bool { return e.cp.Work(e.hostID()).ReplicasKnown == 3 })
	victim := e.pids()["vm_01"][0]
	done := false
	e.cp.OnClaim = func(a hostapi.Assignment) {
		if a.RunID == "run_t7" && !done {
			done = true
			_ = syscall.Kill(victim, syscall.SIGKILL)
			ds := desired(3, "10.200.4.0/23", true, nil)
			ds.VMs = ds.VMs[1:]
			go e.cp.SetDesired(e.hostID(), ds)
			time.Sleep(50 * time.Millisecond)
		}
	}
	e.cp.PreferReplica("rep_01")
	start := time.Now()
	e.cp.Enqueue("agent-x", "run_t7")
	var by string
	e.waitFor(3*time.Second, "re-claim by another replica", func() bool {
		for _, c := range e.cp.Work(e.hostID()).Claims {
			if c.RunID == "run_t7" && c.ReplicaID != "rep_01" {
				by = c.ReplicaID
				return true
			}
		}
		return false
	})
	took := time.Since(start)
	vm := strings.Replace(by, "rep_", "vm_", 1)
	e.waitFor(3*time.Second, "accepted", func() bool { return strings.Contains(strings.Join(e.inboxRuns(vm), ","), "run_t7") })
	w := e.cp.Work(e.hostID())
	if len(w.HandBacks) == 0 || len(w.HandBacks) > 3 {
		t.Fatalf("hand-backs: %v", w.HandBacks)
	}
	t.Logf("backend=%s: run_t7 handed back after rep_01 died, re-claimed by %s in %s (%d hand-back)", e.backend, by, took, len(w.HandBacks))
	_ = context.Background
}
