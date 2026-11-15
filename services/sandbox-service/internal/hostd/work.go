package hostd

import (
	"bufio"
	"context"
	"encoding/json"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/gateway"
	"github.com/seanheiney/boss/services/sandbox-service/internal/guest"
	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
)

// workLoop is the host's ONE outstanding work request (LLD §3.13.0, REVISION
// 2026-11-14): POST /v1/run-plane/hosts/next {wait_ms, state_gen}, re-issued
// at once after every answer. VMs never poll BOSS. A {resync:true} answer
// means BOSS's stored replica-state generation differs from ours: PUT the
// full replica map once, then poll again.
func (a *Agent) workLoop(ctx context.Context) {
	backoff := 250 * time.Millisecond
	for ctx.Err() == nil {
		if a.revoked.Load() || a.headlessExpired() || !a.hasServing() {
			select {
			case <-ctx.Done():
				return
			case <-time.After(500 * time.Millisecond):
			}
			continue
		}
		// Let a pending /observed land first, so BOSS already holds this
		// generation and the poll needs no resync (bounded: ≤ 300 ms).
		for i := 0; i < 6 && a.pushedGen.Load() != a.stateGen.Load(); i++ {
			time.Sleep(50 * time.Millisecond)
		}
		gen := a.stateGen.Load()
		pctx, cancel := context.WithTimeout(ctx, a.cfg.WorkWait+15*time.Second)
		resp, err := a.client.HostsNext(pctx, hostapi.HostsNextRequest{
			WaitMS: int(a.cfg.WorkWait / time.Millisecond), StateGen: gen})
		cancel()
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(backoff):
			}
			if backoff < 5*time.Second {
				backoff *= 2
			}
			continue
		}
		backoff = 250 * time.Millisecond
		a.touchCP()
		if resp.Resync {
			a.resyncReplicas(ctx)
			continue
		}
		// Hand each run to its VM concurrently and poll again at once: one
		// outstanding hosts/next, never blocked by a slow VM (deliveries are
		// bounded by DeliverTimeout and hand back on failure).
		for _, run := range resp.Runs {
			go a.deliver(ctx, run)
		}
	}
}

func (a *Agent) hasServing() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, st := range a.vms {
		if st.getState() == hostapi.StateServing {
			return true
		}
	}
	return false
}

// resyncReplicas PUTs the full replica map with the current generation.
func (a *Agent) resyncReplicas(ctx context.Context) {
	gen := a.stateGen.Load()
	rctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if err := a.client.PutReplicas(rctx, a.HostID(), hostapi.ReplicaMap{
		StateGen: gen, Replicas: a.replicaStatuses(ctx, false)}); err != nil {
		a.logf("replica resync failed: %v", err)
		select {
		case <-ctx.Done():
		case <-time.After(time.Second):
		}
	}
}

// deliver hands one claimed run to its VM's work inbox. A VM that is gone,
// not serving, full by our own ping, refusing, or silent for DeliverTimeout
// gets the run handed back at once, so BOSS re-queues it for another replica.
func (a *Agent) deliver(ctx context.Context, run hostapi.Assignment) (handedBack bool) {
	reason := a.tryDeliver(run)
	if reason == "" {
		return false
	}
	a.correctDrift(run.ReplicaID, reason)
	a.gw.Runs.Delete(run.RunID)
	a.logf("run %s → replica %s refused (%s): handing back", run.RunID, run.ReplicaID, reason)
	a.event(a.vmOfReplica(run.ReplicaID), "handed_back", "", "",
		map[string]string{"run_id": run.RunID, "replica_id": run.ReplicaID, "reason": reason})
	hctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if err := a.client.HandBack(hctx, run); err != nil {
		a.logf("hand back %s failed: %v (the 30 s lease expiry re-queues it)", run.RunID, err)
	}
	return true
}

// correctDrift: BOSS assigned a run to a replica the host cannot serve. If
// BOSS's picture is wrong (an unknown or non-serving replica), push the truth
// now: a tombstone for an unknown replica, a state bump otherwise.
func (a *Agent) correctDrift(replicaID, reason string) {
	switch {
	case reason == "unknown_replica" || reason == "vm_not_running" || reason == "no_free_slot" ||
		reason == "inbox_unreachable":
		// Push the real map (a stopped VM's tombstone, a non-serving state)
		// with a new state_gen; the next poll waits for it to land.
		a.bumpState()
	}
}

func (a *Agent) vmOfReplica(replicaID string) string {
	a.mu.Lock()
	defer a.mu.Unlock()
	for id, st := range a.vms {
		st.mu.Lock()
		rid := st.spec.ReplicaID
		st.mu.Unlock()
		if rid == replicaID {
			return id
		}
	}
	return ""
}

func (a *Agent) tryDeliver(run hostapi.Assignment) string {
	a.mu.Lock()
	var st *vmState
	for _, v := range a.vms {
		v.mu.Lock()
		match := v.spec.ReplicaID == run.ReplicaID && run.ReplicaID != ""
		v.mu.Unlock()
		if match {
			st = v
			break
		}
	}
	a.mu.Unlock()
	if st == nil {
		return "unknown_replica"
	}
	st.mu.Lock()
	vm, free := st.vm, st.freeSlots()
	if free > 0 {
		st.pending++ // reserve locally until the next ping
	}
	st.mu.Unlock()
	if vm == nil {
		return "vm_not_running"
	}
	if free <= 0 {
		return "no_free_slot"
	}
	vm.Taint("run " + run.RunID) // never snapshot a VM that saw a run (S15)
	release := func() {
		st.mu.Lock()
		if st.pending > 0 {
			st.pending--
		}
		st.mu.Unlock()
	}
	deadline := time.Now().Add(a.cfg.DeliverTimeout)
	c, err := a.cfg.Manager.DialInbox(vm, deadline)
	if err != nil {
		release()
		return "inbox_unreachable"
	}
	defer c.Close()
	// The run token stays with the host (gateway run map); the VM gets ids only.
	a.gw.Runs.Put(run.RunID, gateway.RunLease{ReplicaID: run.ReplicaID, JobID: run.JobID,
		Token: run.RunToken, Epoch: run.Epoch})
	w := guest.Work{Op: "run", RunID: run.RunID, JobID: run.JobID, ReplicaID: run.ReplicaID, Epoch: run.Epoch}
	raw, _ := json.Marshal(w)
	if _, err := c.Write(append(raw, '\n')); err != nil {
		release()
		a.gw.Runs.Delete(run.RunID)
		return "inbox_write_failed"
	}
	line, err := bufio.NewReader(c).ReadBytes('\n')
	if err != nil {
		release()
		a.gw.Runs.Delete(run.RunID)
		return "inbox_no_answer"
	}
	var reply guest.InboxReply
	if json.Unmarshal(line, &reply) != nil {
		release()
		a.gw.Runs.Delete(run.RunID)
		return "inbox_bad_answer"
	}
	if !reply.OK {
		release()
		a.gw.Runs.Delete(run.RunID)
		if reply.Reason == "" {
			reply.Reason = "refused"
		}
		return "vm_refused: " + reply.Reason
	}
	return ""
}
