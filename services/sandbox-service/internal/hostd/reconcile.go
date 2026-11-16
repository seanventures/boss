package hostd

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"net"
	"time"

	"github.com/seanventures/boss/services/sandbox-service/internal/cache"
	"github.com/seanventures/boss/services/sandbox-service/internal/gateway"
	"github.com/seanventures/boss/services/sandbox-service/internal/guest"
	"github.com/seanventures/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanventures/boss/services/sandbox-service/internal/netpol"
	"github.com/seanventures/boss/services/sandbox-service/internal/vmm"
)

// adoptRunning rebuilds the in-memory view from what is actually running:
// a process scan (VMs) and a netlink scan (taps). Nothing is stopped here —
// orphans are only stopped once a desired state proves them unwanted.
func (a *Agent) adoptRunning(ctx context.Context) error {
	found, err := a.cfg.Manager.Scan()
	if err != nil {
		return err
	}
	cached := map[string]cache.VMRecord{}
	if a.cache != nil {
		cached, _ = a.cache.VMs()
	}
	a.mu.Lock()
	for _, vm := range found {
		if _, dup := a.vms[vm.ID]; dup {
			// Two processes claim one VM id: keep the first, stop the rest.
			a.mu.Unlock()
			a.logf("duplicate VM process for %s (pid %d): stopping it", vm.ID, vm.PID)
			_ = a.cfg.Manager.Stop(ctx, vm)
			a.mu.Lock()
			continue
		}
		if vm.Spec != nil && vm.Spec.Warm {
			// A warm VM has no identity yet: back into the pool it goes.
			sp := *vm.Spec
			w := &warmVM{slot: vm.ID, vm: vm, ready: true, since: time.Now(),
				key: poolKey{sp.DeploymentID, a.shape(sp)}}
			if sp.Net != nil {
				w.tap = sp.Net.Tap
			}
			a.warm[vm.ID] = w
			continue
		}
		st := &vmState{id: vm.ID, vm: vm, state: "adopted", startedAt: time.Now()}
		if rec, ok := cached[vm.ID]; ok {
			for _, ts := range rec.Restarts {
				st.restarts = append(st.restarts, time.Unix(ts, 0))
			}
		}
		a.vms[vm.ID] = st
	}
	a.mu.Unlock()
	if taps, err := a.cfg.Net.Taps(ctx); err == nil {
		a.mu.Lock()
		for _, t := range taps {
			a.taps[t] = true
		}
		a.mu.Unlock()
	}
	if len(found) > 0 {
		a.logf("adopted %d running VM(s) from the process scan", len(found))
	}
	return nil
}

func (a *Agent) reconcileLoop(ctx context.Context) {
	t := time.NewTicker(a.cfg.ReconcileEvery)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case id := <-a.unhealthy:
			a.handleUnhealthy(ctx, id)
		case <-a.kick:
		case <-t.C:
		}
		a.Reconcile(ctx)
	}
}

// Reconcile converges observed VMs toward the desired state. It is the only
// goroutine that starts or stops VMs.
func (a *Agent) Reconcile(ctx context.Context) {
	a.mu.Lock()
	ds, live := a.desired, a.desiredLive
	a.mu.Unlock()
	if ds == nil {
		return // never act destructively before the first desired state
	}
	want := map[string]hostapi.DesiredVM{}
	var wanted []hostapi.DesiredVM
	for _, dv := range ds.VMs {
		if dv.Running() {
			want[dv.VMID] = dv
			wanted = append(wanted, dv)
		}
	}
	a.mu.Lock()
	var extras []string
	for id := range a.vms {
		if _, ok := want[id]; !ok {
			extras = append(extras, id)
		}
	}
	for id := range a.refused {
		if _, ok := want[id]; !ok {
			delete(a.refused, id)
		}
	}
	a.mu.Unlock()
	if !live {
		extras = nil // headless on the cached state: keep, never stop
	}
	for _, id := range extras {
		a.stopVM(ctx, id, "not in desired state")
	}

	for _, dv := range wanted {
		a.mu.Lock()
		st := a.vms[dv.VMID]
		a.mu.Unlock()
		if st == nil {
			if reason := a.admissible(ds, dv); reason != "" {
				a.refuse(dv, reason)
				continue
			}
			a.clearRefused(dv.VMID)
			a.startVM(ctx, dv, nil)
			continue
		}
		st.mu.Lock()
		changed := st.haveSpec && (st.spec.ImageDigest != dv.ImageDigest || st.spec.Mode != dv.Mode ||
			st.spec.VMIP != dv.VMIP || st.spec.Resources.VCPU() != dv.Resources.VCPU() ||
			st.spec.Resources.MemMiB() != dv.Resources.MemMiB())
		st.mu.Unlock()
		if changed {
			// P8a replaces in place; P8b rolls with surge 1 behind the VIP.
			// A changed spec also clears a crashloop or quarantine.
			a.stopVM(ctx, dv.VMID, "spec changed")
			continue
		}
		switch st.getState() {
		case hostapi.StateCrashloop:
			st.mu.Lock()
			retry := time.Now().After(st.crashloopUntil)
			st.mu.Unlock()
			if retry {
				// One replacement attempt, keeping the history: if it fails
				// too, the VM is quarantined (LLD §3.3).
				a.logf("VM %s: crashloop window elapsed; one replacement attempt", dv.VMID)
				a.startVM(ctx, dv, st)
			}
			continue
		case hostapi.StateQuarantined, hostapi.StateUnhealthy:
			continue // quarantine waits for a spec change; unhealthy is the watchdog's
		}
		a.ensureAttached(ctx, st, dv)
	}

	a.fillPools(ctx, ds, live)
	a.gcSnapshots(ds)

	// Sweep taps that no running VM owns (left by a crash).
	a.mu.Lock()
	owned := map[string]bool{}
	for _, st := range a.vms {
		if st.tap != "" {
			owned[st.tap] = true
		}
	}
	for _, w := range a.warm {
		if w.tap != "" {
			owned[w.tap] = true
		}
	}
	liveIDs := map[string]bool{}
	for _, st := range a.vms {
		if st.vm != nil {
			liveIDs[vmm.FirecrackerID(st.vm.ID)] = true
		}
	}
	for _, w := range a.warm {
		if w.vm != nil {
			liveIDs[vmm.FirecrackerID(w.vm.ID)] = true
		}
	}
	var orphanTaps []string
	for t := range a.taps {
		if !live {
			break // sweep only against a live desired state
		}
		if !owned[t] {
			orphanTaps = append(orphanTaps, t)
		}
		delete(a.taps, t)
	}
	a.mu.Unlock()
	for _, t := range orphanTaps {
		if err := a.cfg.Net.Remove(ctx, t); err == nil {
			a.event("", "orphan_tap_removed", "", "", map[string]string{"tap": t})
		}
	}
	if sw, ok := a.cfg.Manager.(interface{ SweepNetns(map[string]bool) }); ok && live {
		sw.SweepNetns(liveIDs)
	}
	if live {
		a.observed.Store(ds.Generation)
	}
}

func (a *Agent) refuse(dv hostapi.DesiredVM, reason string) {
	id := dv.VMID
	a.mu.Lock()
	prev := a.refused[id].reason
	a.refused[id] = refusal{replicaID: dv.ReplicaID, reason: reason}
	a.mu.Unlock()
	if prev != reason {
		a.logf("refused VM %s: %s", id, reason)
		a.event(id, "refused", "", hostapi.StateRefused, map[string]string{"reason": reason})
	}
}

func (a *Agent) clearRefused(id string) {
	a.mu.Lock()
	delete(a.refused, id)
	a.mu.Unlock()
}

// admissible returns "" or the named reason a desired VM is refused.
func (a *Agent) admissible(ds *hostapi.DesiredState, dv hostapi.DesiredVM) string {
	switch {
	case !vmm.VMIDPattern.MatchString(dv.VMID):
		return "invalid_vm_id"
	case dv.Mode != hostapi.ModeReplica && dv.Mode != hostapi.ModePerRun:
		return "invalid_vm_mode"
	case a.revoked.Load():
		return "host_revoked"
	case dv.Mode == hostapi.ModePerRun && (a.cfg.ClaimsFrozen || ds.ClaimsFrozen):
		return "microvm_claims_frozen"
	}
	if dv.Mode == hostapi.ModeReplica && a.cfg.Manager.Name() == "firecracker" {
		ip := net.ParseIP(dv.VMIP).To4()
		c := a.creds.Load()
		_, prefix, err := net.ParseCIDR(c.VMPrefix)
		if ip == nil || err != nil || !prefix.Contains(ip) {
			return "vm_ip_outside_prefix"
		}
	}
	a.mu.Lock()
	running := len(a.vms)
	a.mu.Unlock()
	if running >= a.cfg.MaxVMs {
		return "capacity"
	}
	return ""
}

func (a *Agent) vmSpec(dv hostapi.DesiredVM) vmm.Spec {
	s := vmm.Spec{VMID: dv.VMID, DeploymentID: dv.DeploymentID, DeploymentVersion: dv.AgentVersion,
		Mode: dv.Mode, ImageDigest: dv.ImageDigest, VCPU: dv.Resources.VCPU(), MemMiB: dv.Resources.MemMiB(),
		Slots: dv.Slots}
	if dv.Mode == hostapi.ModeReplica {
		s.VMIP = dv.VMIP
	}
	if dv.Mode == hostapi.ModeReplica && a.cfg.Manager.Name() == "firecracker" {
		s.Net = &vmm.NetAttach{Tap: netpol.TapName(dv.VMID), MAC: netpol.MACFor(dv.VMIP),
			VMIP: dv.VMIP, Gateway: netpol.Gateway}
	}
	// per_run: Net stays nil — the VM boots with no network device at all.
	return s
}

// startVM creates a VM (cold boot in P8a; Restore in P8b) and attaches it.
func (a *Agent) startVM(ctx context.Context, dv hostapi.DesiredVM, prior *vmState) {
	st := prior
	if st == nil {
		st = &vmState{id: dv.VMID}
	}
	st.mu.Lock()
	st.spec, st.haveSpec = dv, true
	st.startedAt, st.lastPing, st.misses, st.bootstrapped, st.vm = time.Now(), time.Time{}, 0, false, nil
	st.servingSince, st.busySlots, st.totalSlots, st.pending = time.Time{}, 0, 0, 0
	st.mu.Unlock()
	st.setState(hostapi.StateProvisioning, "")
	a.mu.Lock()
	a.vms[dv.VMID] = st
	delete(a.gone, dv.VMID)
	a.mu.Unlock()

	spec := a.vmSpec(dv)
	spec.RuntimeVersion = a.cfg.RuntimeVersion
	claimStart := time.Now()
	owner, _ := a.cfg.Manager.(vmm.LinkOwner)
	ownsLink := owner != nil && owner.OwnsReplicaLink()

	// Warm claim (LLD §3.3 "restoring → ready"): a ready VM of this shape
	// takes the identity; nothing boots.
	if w := a.takeWarm(poolKey{dv.DeploymentID, a.shape(spec)}); w != nil {
		st.setState(hostapi.StateRestoring, "")
		a.event(dv.VMID, "vm_state", hostapi.StateProvisioning, hostapi.StateRestoring,
			map[string]string{"mode": dv.Mode, "warm": w.slot})
		if spec.Net != nil {
			spec.Net.Tap = w.tap // the spare's link carries this replica's policy
		}
		w.vm.Claim(spec)
		if spec.Net != nil {
			if err := a.ensureNet(ctx, spec); err != nil {
				_ = a.cfg.Manager.Stop(ctx, w.vm)
				_ = a.cfg.Net.Remove(ctx, w.tap)
				a.fail(st, "net_setup_failed: "+err.Error())
				return
			}
			st.mu.Lock()
			st.tap = w.tap
			st.mu.Unlock()
		}
		st.mu.Lock()
		st.vm, st.claimStart = w.vm, claimStart
		st.mu.Unlock()
		a.persist(st)
		a.attach(ctx, st)
		return
	}

	// No spare: restore from the shape's snapshot, or cold boot (and take the
	// first snapshot). A link-owning backend builds the replica link at
	// Create, so the policy follows; a plain tap is created first.
	st.setState(hostapi.StateBooting, "")
	a.event(dv.VMID, "vm_state", hostapi.StateProvisioning, hostapi.StateBooting, map[string]string{"mode": dv.Mode})
	if spec.Net != nil && !ownsLink {
		if err := a.ensureNet(ctx, spec); err != nil {
			a.fail(st, "net_setup_failed: "+err.Error())
			return
		}
		st.mu.Lock()
		st.tap = spec.Net.Tap
		st.mu.Unlock()
	}
	vm, kind, err := a.boot(ctx, spec)
	if err != nil {
		a.fail(st, kind+"_failed: "+err.Error())
		return
	}
	if spec.Net != nil && ownsLink {
		if err := a.ensureNet(ctx, spec); err != nil {
			_ = a.cfg.Manager.Stop(ctx, vm)
			a.fail(st, "net_setup_failed: "+err.Error())
			return
		}
	}
	st.mu.Lock()
	st.vm, st.claimStart = vm, claimStart
	if spec.Net != nil {
		st.tap = spec.Net.Tap
	}
	st.mu.Unlock()
	a.persist(st)
	a.attach(ctx, st)
}

// ensureNet applies the replica's network policy to its host link.
func (a *Agent) ensureNet(ctx context.Context, spec vmm.Spec) error {
	c := a.creds.Load()
	dv := a.desiredOf(spec.VMID)
	return a.cfg.Net.Ensure(ctx, netpol.VMNet{VMID: spec.VMID, Tap: spec.Net.Tap, VMIP: spec.VMIP,
		VIP: dv.VIP, VIPPorts: dv.VIPPorts, OwnerUID: a.cfg.TapOwnerUID, VMPrefix: c.VMPrefix})
}

func (a *Agent) desiredOf(vmID string) hostapi.DesiredVM {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.desired != nil {
		for _, dv := range a.desired.VMs {
			if dv.VMID == vmID {
				return dv
			}
		}
	}
	return hostapi.DesiredVM{}
}

// fail records a start failure and routes it through the restart policy.
func (a *Agent) fail(st *vmState, reason string) {
	a.logf("VM %s: %s", st.id, reason)
	st.setState(hostapi.StateUnhealthy, reason)
	select {
	case a.unhealthy <- st.id:
	default:
	}
}

func (a *Agent) persist(st *vmState) {
	if a.cache == nil {
		return
	}
	st.mu.Lock()
	rec := cache.VMRecord{VMID: st.id, DeploymentID: st.spec.DeploymentID, Mode: st.spec.Mode,
		Tap: st.tap, VMIP: st.spec.VMIP, ImageDigest: st.spec.ImageDigest, StartedAt: st.startedAt}
	if st.vm != nil {
		rec.PID, rec.Dir = st.vm.PID, st.vm.Dir
	}
	for _, t := range st.restarts {
		rec.Restarts = append(rec.Restarts, t.Unix())
	}
	st.mu.Unlock()
	_ = a.cache.PutVM(rec)
}

// ensureAttached makes an adopted (or running) VM whole again after a host
// restart: spec, tap rules, gateway listeners and watchdog. It never restarts
// a healthy VM.
func (a *Agent) ensureAttached(ctx context.Context, st *vmState, dv hostapi.DesiredVM) {
	st.mu.Lock()
	adopted := !st.haveSpec
	st.spec, st.haveSpec = dv, true
	watching := st.stopWatch != nil
	vm := st.vm
	st.mu.Unlock()
	if vm == nil {
		return
	}
	if adopted {
		spec := a.vmSpec(dv)
		// What is running must be what is desired: compare the VM's own
		// spec record (boss-spec.json, written at Create; bbolt may be gone).
		if diff := vmm.Differs(vm.Spec, spec); diff != "" {
			a.logf("adopted VM %s differs from desired (%s): replacing", dv.VMID, diff)
			a.event(dv.VMID, "adopted_mismatch", "", "", map[string]string{"field": diff})
			a.stopVM(ctx, dv.VMID, "adopted VM differs from desired: "+diff)
			return
		}
		if spec.Net != nil {
			c := a.creds.Load()
			if err := a.cfg.Net.Ensure(ctx, netpol.VMNet{VMID: dv.VMID, Tap: spec.Net.Tap, VMIP: dv.VMIP,
				VIP: dv.VIP, VIPPorts: dv.VIPPorts, OwnerUID: a.cfg.TapOwnerUID, VMPrefix: c.VMPrefix}); err != nil {
				a.logf("VM %s: re-apply net policy: %v", dv.VMID, err)
			}
			st.mu.Lock()
			st.tap = spec.Net.Tap
			st.mu.Unlock()
		}
		a.persist(st)
		a.event(dv.VMID, "adopted", "", "", map[string]string{"pid": fmt.Sprint(vm.PID)})
	}
	if !watching {
		a.attach(ctx, st)
	}
}

func (a *Agent) attach(ctx context.Context, st *vmState) {
	st.mu.Lock()
	vm, dv := st.vm, st.spec
	st.mu.Unlock()
	if a.gw != nil {
		err := a.gw.Attach(st.id, gateway.VMInfo{DeploymentID: dv.DeploymentID,
			DeploymentVersion: dv.AgentVersion, ImageDigest: dv.ImageDigest},
			func(port int) (net.Listener, error) { return a.cfg.Manager.GuestListen(vm, port) })
		if err != nil {
			a.fail(st, "gateway_attach_failed: "+err.Error())
			return
		}
	}
	wctx, cancel := context.WithCancel(ctx)
	st.mu.Lock()
	st.stopWatch = cancel
	st.mu.Unlock()
	go a.watchdog(wctx, st, vm)
}

// stopVM withdraws (P8b: directors), detaches, stops, and removes the tap.
func (a *Agent) stopVM(ctx context.Context, id, why string) {
	a.mu.Lock()
	st := a.vms[id]
	delete(a.vms, id)
	a.mu.Unlock()
	if st == nil {
		return
	}
	st.cancelWatch()
	if a.gw != nil {
		a.gw.Detach(id)
	}
	st.mu.Lock()
	vm, tap, prev := st.vm, st.tap, st.state
	spec := st.spec
	st.mu.Unlock()
	if prev == hostapi.StateServing || prev == hostapi.StateDraining || prev == hostapi.StateUnhealthy {
		a.withdrawVM(spec, "stopped: "+why, true) // directors: weight 0, then delete
	}
	st.mu.Lock()
	tomb := hostapi.ReplicaStatus{ReplicaID: st.spec.ReplicaID, VMID: id, DeploymentID: st.spec.DeploymentID,
		Mode: st.spec.Mode, State: hostapi.StateStopped, Reason: why, VsockPingAgeMS: -1}
	st.mu.Unlock()
	a.mu.Lock()
	a.gone[id] = tomb
	a.mu.Unlock()
	if vm != nil {
		if err := a.cfg.Manager.Stop(ctx, vm); err != nil {
			a.logf("stop %s: %v", id, err)
		}
	}
	if tap != "" {
		if err := a.cfg.Net.Remove(ctx, tap); err != nil {
			a.logf("remove tap %s: %v", tap, err)
		}
	}
	if a.cache != nil {
		_ = a.cache.DeleteVM(id)
	}
	a.event(id, "vm_state", prev, hostapi.StateStopped, map[string]string{"why": why})
}

// handleUnhealthy applies the restart policy: at most RestartMax restarts in
// RestartWindow, then crashloop (LLD §3.16.1).
func (a *Agent) handleUnhealthy(ctx context.Context, id string) {
	a.mu.Lock()
	st := a.vms[id]
	a.mu.Unlock()
	if st == nil {
		return
	}
	st.cancelWatch()
	if a.gw != nil {
		a.gw.Detach(id)
	}
	st.mu.Lock()
	vm, reason, dv := st.vm, st.reason, st.spec
	st.mu.Unlock()
	// Withdraw first (P8b-10): weight 0 at every director at once; on a
	// confirmed death (process gone, pings silent) the real server is also
	// deleted so its connections are reset and clients reconnect elsewhere.
	a.withdrawVM(dv, reason, confirmedDeath(reason))
	st.mu.Lock()
	if st.crashloops > 0 {
		// The replacement after a crashloop failed too: quarantine. Nothing
		// restarts it until its spec changes or it leaves desired state.
		st.mu.Unlock()
		if vm != nil {
			_ = a.cfg.Manager.Stop(ctx, vm)
		}
		st.setState(hostapi.StateQuarantined, "replacement after crashloop also failed: "+reason)
		a.persist(st)
		a.logf("VM %s QUARANTINED (%s)", id, reason)
		a.event(id, "quarantined", hostapi.StateUnhealthy, hostapi.StateQuarantined, map[string]string{"reason": reason})
		return
	}
	now := time.Now()
	kept := st.restarts[:0]
	for _, t := range st.restarts {
		if now.Sub(t) < a.cfg.RestartWindow {
			kept = append(kept, t)
		}
	}
	st.restarts = kept
	crash := len(st.restarts) >= a.cfg.RestartMax
	if crash {
		st.crashloopUntil = st.restarts[0].Add(a.cfg.RestartWindow)
		st.crashloops++
	} else {
		st.restarts = append(st.restarts, now)
	}
	restarts := len(st.restarts)
	st.mu.Unlock()
	a.event(id, "vm_state", "", hostapi.StateUnhealthy, map[string]string{"reason": reason})
	if vm != nil {
		_ = a.cfg.Manager.Stop(ctx, vm)
	}
	if crash {
		st.setState(hostapi.StateCrashloop, fmt.Sprintf("%d restarts in %s; last: %s", restarts, a.cfg.RestartWindow, reason))
		a.persist(st)
		a.logf("VM %s is in CRASHLOOP (%s)", id, reason)
		a.event(id, "crashloop", hostapi.StateUnhealthy, hostapi.StateCrashloop,
			map[string]string{"reason": reason, "restarts": fmt.Sprint(restarts)})
		return
	}
	a.logf("VM %s unhealthy (%s): restart %d/%d", id, reason, restarts, a.cfg.RestartMax)
	a.event(id, "restart", hostapi.StateUnhealthy, hostapi.StateBooting, map[string]string{"attempt": fmt.Sprint(restarts)})
	a.startVM(ctx, dv, st)
}

// watchdog pings the VM over its control channel every PingEvery. A process
// exit is noticed at once; PingMisses missed pings (after the boot grace)
// mean unhealthy. The first answered exchange is the bootstrap.
func (a *Agent) watchdog(ctx context.Context, st *vmState, vm *vmm.VM) {
	t := time.NewTicker(a.cfg.PingEvery)
	defer t.Stop()
	report := func(reason string) {
		st.setState(hostapi.StateUnhealthy, reason)
		select {
		case a.unhealthy <- st.id:
		case <-ctx.Done():
		}
	}
	for first := true; ; first = false {
		if first {
			// The first exchange (the bootstrap) goes out at once: a warm
			// claim must not wait a whole tick for its identity.
			select {
			case <-ctx.Done():
				return
			case <-vm.Exited:
				if ctx.Err() == nil {
					report("process_exit")
				}
				return
			default:
			}
		} else {
			select {
			case <-ctx.Done():
				return
			case <-vm.Exited:
				if ctx.Err() == nil {
					report("process_exit")
				}
				return
			case <-t.C:
			}
		}
		ok := a.exchange(st, vm)
		st.mu.Lock()
		if ok {
			st.lastPing, st.misses = time.Now(), 0
		} else if !st.lastPing.IsZero() || time.Since(st.startedAt) > a.cfg.BootGrace {
			st.misses++
		}
		misses, state := st.misses, st.state
		weight := st.spec.Weight
		mode := st.spec.Mode
		st.mu.Unlock()
		if ok && state != hostapi.StateServing && state != hostapi.StateReady {
			to := hostapi.StateReady
			if mode == hostapi.ModePerRun || weight > 0 || mode == hostapi.ModeReplica {
				// P8a has no directors to register with: a healthy replica
				// serves the internal pull loop; P8b gates this on VIP
				// registration and the TLS self-check.
				to = hostapi.StateServing
			}
			prev := st.setState(to, "")
			a.event(st.id, "vm_state", prev, to, nil)
			if to == hostapi.StateServing {
				a.registerVM(st.spec) // directors: this real server takes traffic now
			}
		}
		if ok {
			st.mu.Lock()
			if st.servingSince.IsZero() {
				st.servingSince = time.Now()
			} else if st.crashloops > 0 && time.Since(st.servingSince) > a.cfg.RestartWindow {
				st.crashloops, st.restarts = 0, nil // healthy for a full window: history cleared
			}
			st.mu.Unlock()
		}
		if misses >= a.cfg.PingMisses {
			report(fmt.Sprintf("missed_pings: %d", misses))
			return
		}
	}
}

func (a *Agent) exchange(st *vmState, vm *vmm.VM) bool {
	// The whole ping (dial, handshake, exchange) fits inside one tick, so
	// PingMisses misses take at most PingMisses × PingEvery.
	deadline := time.Now().Add(a.cfg.PingEvery * 9 / 10)
	c, err := a.cfg.Manager.DialControl(vm, deadline)
	if err != nil {
		return false
	}
	defer c.Close()
	st.mu.Lock()
	boot := !st.bootstrapped
	dv := st.spec
	st.mu.Unlock()
	req := guest.Request{Op: "ping"}
	if boot {
		seed := make([]byte, 32)
		_, _ = rand.Read(seed)
		vm.Taint("identity") // from here on this VM is never snapshotted (S15)
		b := &guest.Bootstrap{VMID: dv.VMID, Mode: dv.Mode, Seed: seed, Slots: dv.Slots, RunHoldMS: a.cfg.RunHoldMS,
			CID: vmm.GuestCID(vm.ID), HostTimeUnixNano: time.Now().UnixNano(), Restored: vm.Restored}
		if dv.Mode == hostapi.ModeReplica {
			b.VMIP, b.Gateway, b.VIP, b.MAC = dv.VMIP, netpol.Gateway, dv.VIP, netpol.MACFor(dv.VMIP)
			if dv.ReplicaHandle != "" {
				b.Handles = map[string]string{"replica": dv.ReplicaHandle}
			}
		}
		if dv.Mode == hostapi.ModePerRun && len(dv.Runs) > 0 {
			h := dv.Runs[0].Handles
			b.Handles = map[string]string{"model": h.Model, "egress": h.Egress, "mcp": h.MCP, "run": h.Run}
		}
		req = guest.Request{Op: "bootstrap", Bootstrap: b}
	}
	resp, err := guest.Exchange(c, req, deadline)
	if err != nil || !resp.OK {
		if resp != nil && resp.Error != "" {
			a.logf("VM %s control: %s", st.id, resp.Error)
		}
		return false
	}
	if resp.VMID != "" && resp.VMID != st.id {
		a.logf("VM %s answered as %s: refusing", st.id, resp.VMID)
		return false
	}
	busy, total := resp.BusySlots, resp.TotalSlots
	if total == 0 && resp.Bootstrapped {
		// boss-runtime owns the inbox (vsockd --no-inbox): ask it directly,
		// inside the same tick deadline (HOST_API.md §3.2, §4.5).
		if b, t, ok := a.inboxSlots(vm, deadline); ok {
			busy, total = b, t
		}
	}
	st.mu.Lock()
	if boot {
		st.bootstrapped = true
		if !st.claimStart.IsZero() {
			kind := "cold_claim"
			if vm.Restored {
				kind = "warm_claim"
			}
			a.timing(st.id, kind, st.claimStart)
			st.claimStart = time.Time{}
		}
	}
	slotsChanged := st.totalSlots != total
	st.busySlots, st.totalSlots = busy, total
	st.pending = 0 // the ping is the truth again
	st.mu.Unlock()
	if slotsChanged {
		a.bumpState() // total slots are replica state; busy is BOSS's own count
	}
	return true
}

// Control runs one control op against a VM (diagnostics and tests).
func (a *Agent) Control(id string, op string) (*guest.Response, error) {
	a.mu.Lock()
	st := a.vms[id]
	a.mu.Unlock()
	if st == nil || st.vm == nil {
		return nil, fmt.Errorf("vm %s not running", id)
	}
	deadline := time.Now().Add(5 * time.Second)
	c, err := a.cfg.Manager.DialControl(st.vm, deadline)
	if err != nil {
		return nil, err
	}
	defer c.Close()
	return guest.Exchange(c, guest.Request{Op: op}, deadline)
}

func (a *Agent) shutdownGateway() {
	a.mu.Lock()
	ids := make([]string, 0, len(a.vms))
	for id, st := range a.vms {
		ids = append(ids, id)
		st.cancelWatch()
	}
	a.mu.Unlock()
	if a.gw != nil {
		for _, id := range ids {
			a.gw.Detach(id)
		}
	}
	// VMs keep running: a hostd restart must not take them down.
}

// VMs is every VM's status (for /v1/vms and tests).
func (a *Agent) VMs() []hostapi.ReplicaStatus {
	return a.replicaStatuses(context.Background(), false)
}

// Capacity is (max, used) for the legacy /health endpoint.
func (a *Agent) Capacity() (int, int) {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.cfg.MaxVMs, len(a.vms)
}

// inboxSlots asks a VM's work inbox {"op":"slots"}.
func (a *Agent) inboxSlots(vm *vmm.VM, deadline time.Time) (busy, total int, ok bool) {
	c, err := a.cfg.Manager.DialInbox(vm, deadline)
	if err != nil {
		return 0, 0, false
	}
	defer c.Close()
	if _, err := c.Write([]byte(`{"op":"slots"}` + "\n")); err != nil {
		return 0, 0, false
	}
	line, err := bufio.NewReader(c).ReadBytes('\n')
	if err != nil {
		return 0, 0, false
	}
	var r guest.InboxReply
	if json.Unmarshal(line, &r) != nil || !r.OK || r.BusySlots == nil || r.TotalSlots == nil {
		return 0, 0, false
	}
	return *r.BusySlots, *r.TotalSlots, true
}
