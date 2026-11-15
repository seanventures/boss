package hostd

import (
	"context"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/cpclient"
	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
)

// watchLoop long-polls desired state. Level-based: each changed answer is the
// full state. After any error the next call uses since=0, a full resync.
func (a *Agent) watchLoop(ctx context.Context) {
	var since int64
	backoff := time.Second
	for ctx.Err() == nil {
		ds, err := a.client.Desired(ctx, a.HostID(), since, a.cfg.WatchWait)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			if cpclient.IsRevoked(err) {
				a.setRevoked()
			}
			since = 0 // reconnect = full resync
			select {
			case <-ctx.Done():
				return
			case <-time.After(backoff):
			}
			if backoff < 10*time.Second {
				backoff *= 2
			}
			continue
		}
		backoff = time.Second
		a.touchCP()
		if ds == nil {
			continue // no change within the wait
		}
		a.applyDesired(ctx, ds)
		since = ds.Generation
	}
}

func (a *Agent) applyDesired(ctx context.Context, ds *hostapi.DesiredState) {
	a.mu.Lock()
	first := a.desired == nil || !a.desiredLive
	var prevDirectors []string
	if a.desired != nil {
		prevDirectors = a.desired.Directors
	}
	a.desired = ds
	a.desiredLive = true
	a.mu.Unlock()
	a.gw.Update(*ds)
	if a.cache != nil {
		_ = a.cache.PutDesired(*ds)
	}
	if first || !sameStrings(prevDirectors, ds.Directors) {
		if err := a.cfg.Net.SetDirectors(ctx, ds.Directors); err != nil {
			a.logf("set directors: %v", err)
		}
	}
	a.Kick()
}

// Kick asks the reconciler to run now.
func (a *Agent) Kick() {
	select {
	case a.kick <- struct{}{}:
	default:
	}
}

func (a *Agent) heartbeatLoop(ctx context.Context) {
	t := time.NewTicker(a.cfg.HeartbeatEvery)
	defer t.Stop()
	for {
		_ = a.heartbeatOnce(ctx)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// heartbeatOnce sends one heartbeat; a revoked refusal flips the host into
// the revoked state, and "renew": true triggers an early renewal.
func (a *Agent) heartbeatOnce(ctx context.Context) error {
	hb := a.Heartbeat(ctx)
	hctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	resp, err := a.client.Heartbeat(hctx, a.HostID(), hb)
	if err == nil {
		a.clearGone(hb.Replicas)
	}
	if err != nil {
		if cpclient.IsRevoked(err) {
			a.setRevoked()
		}
		return err
	}
	a.touchCP()
	if resp.Renew {
		select {
		case a.renewNow <- struct{}{}:
		default:
		}
	}
	return nil
}

// observedLoop pushes replica states to /observed as soon as one changes
// (debounced 100 ms), so watchdog transitions do not wait for a heartbeat.
func (a *Agent) observedLoop(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case <-a.pushObs:
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(100 * time.Millisecond):
		}
		octx, cancel := context.WithTimeout(ctx, 5*time.Second)
		gen := a.stateGen.Load()
		reps := a.replicaStatuses(ctx, false)
		err := a.client.Observed(octx, a.HostID(), hostapi.ObservedRequest{
			Replicas: reps, ObservedGeneration: a.observed.Load(), StateGen: gen})
		cancel()
		if err == nil {
			a.pushedGen.Store(gen)
			a.clearGone(reps)
		}
		if cpclient.IsRevoked(err) {
			a.setRevoked()
		}
	}
}

// replicaStatuses is every VM's status plus the refused ones.
func (a *Agent) replicaStatuses(ctx context.Context, readCounters bool) []hostapi.ReplicaStatus {
	a.mu.Lock()
	states := make([]*vmState, 0, len(a.vms))
	for _, v := range a.vms {
		states = append(states, v)
	}
	refused := map[string]refusal{}
	for k, v := range a.refused {
		refused[k] = v
	}
	a.mu.Unlock()
	out := make([]hostapi.ReplicaStatus, 0, len(states)+len(refused))
	for _, v := range states {
		v.mu.Lock()
		tap := v.tap
		v.mu.Unlock()
		if readCounters && tap != "" {
			if c, err := a.cfg.Net.Counters(ctx, tap); err == nil {
				v.setDrops(c.Originated + c.Metadata)
			}
		}
		out = append(out, v.status())
	}
	for id, r := range refused {
		out = append(out, hostapi.ReplicaStatus{ReplicaID: r.replicaID, VMID: id, State: hostapi.StateRefused,
			Reason: r.reason, VsockPingAgeMS: -1})
	}
	a.mu.Lock()
	for _, g := range a.gone {
		out = append(out, g)
	}
	a.mu.Unlock()
	return out
}

// clearGone drops tombstones that a successful push has delivered.
func (a *Agent) clearGone(sent []hostapi.ReplicaStatus) {
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, r := range sent {
		if r.State != hostapi.StateStopped {
			continue
		}
		for k, g := range a.gone {
			if g.VMID == r.VMID && g.ReplicaID == r.ReplicaID && g.Reason == r.Reason {
				delete(a.gone, k)
			}
		}
	}
}

// Heartbeat builds the current heartbeat body.
func (a *Agent) Heartbeat(ctx context.Context) hostapi.HeartbeatRequest {
	a.mu.Lock()
	a.hbCount++
	readCounters := a.hbCount%5 == 1 // nft counters every ~10 s
	frozen := a.cfg.ClaimsFrozen
	pools := map[string]hostapi.PoolDepth{}
	if a.desired != nil {
		frozen = frozen || a.desired.ClaimsFrozen
		for _, p := range a.desired.Pools {
			pools[p.DeploymentID] = hostapi.PoolDepth{Depth: 0, MinWarm: p.MinWarm}
		}
	}
	running := len(a.vms)
	a.mu.Unlock()

	replicas := a.replicaStatuses(ctx, readCounters)
	status := "ready"
	switch {
	case a.revoked.Load():
		status = "revoked"
	case a.headlessExpired():
		status = hostapi.StateHeadless
	case time.Since(time.Unix(0, a.lastCP.Load())) > 3*a.cfg.HeartbeatEvery+a.cfg.WatchWait:
		status = "headless"
	}
	capacity := a.capacity()
	capacity.UsedVMs = running
	alloc := capacity.MaxVMs - running
	if alloc < 0 || status != "ready" {
		alloc = 0
	}
	return hostapi.HeartbeatRequest{
		Facts: a.facts(ctx), Capacity: capacity, Allocatable: hostapi.Allocatable{VMs: alloc},
		Pressure: pressure(a.cfg.StateDir), Replicas: replicas, WarmPools: pools, BGPState: "none",
		ObservedGeneration: a.observed.Load(), Status: status, Backend: a.cfg.Manager.Name(),
		ClaimsFrozen: frozen, ContractVersion: hostapi.Version,
	}
}

func sameStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
