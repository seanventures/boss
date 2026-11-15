package hostd

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/guest"
	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanheiney/boss/services/sandbox-service/internal/netpol"
	"github.com/seanheiney/boss/services/sandbox-service/internal/vmm"
)

// Warm pools and snapshots (LLD §2.10, §3.4).
//
// A warm VM is a VM of one shape, restored from the shape's snapshot (or cold
// booted the first time, which also produces the snapshot), running with no
// identity: it has answered a control ping but never a bootstrap. It is known
// by a host-local slot id. When a desired VM of that shape arrives, the host
// takes a warm VM, gives it the control-plane identity over vsock (vm_id,
// seed, replica network, handles) and attaches it: the "warm claim".
//
// Pool sizes: per_run pools come from desired state (min_warm, default 2,
// nothing while claims are frozen); each replica shape keeps ReplicaSurge
// (default 1) spare so a replacement or roll is a restore away.
//
// Hygiene (S15): a VM is tainted the moment it is sent a bootstrap (identity,
// handles) or a run; Snapshot refuses tainted VMs, so a snapshot is only ever
// taken at "runtime imported, waiting for identity".

type poolKey struct {
	DeploymentID string
	Shape        vmm.Shape
}

type poolTarget struct {
	min  int
	spec vmm.Spec // template: VMID empty
	mode string
}

type warmVM struct {
	slot  string
	key   poolKey
	vm    *vmm.VM
	tap   string // replica spare: the host-side link
	ready bool
	since time.Time
}

// newSlot mints a host-local warm VM id (never a control-plane vm_id).
func newSlot() string {
	b := make([]byte, 4)
	_, _ = rand.Read(b)
	return "w-" + hex.EncodeToString(b)
}

// poolTargets derives the wanted warm depth per (deployment, shape).
func (a *Agent) poolTargets(ds *hostapi.DesiredState) map[poolKey]poolTarget {
	out := map[poolKey]poolTarget{}
	if ds == nil {
		return out
	}
	frozen := a.cfg.ClaimsFrozen || ds.ClaimsFrozen
	for _, p := range ds.Pools {
		if frozen || a.revoked.Load() {
			continue
		}
		spec := vmm.Spec{DeploymentID: p.DeploymentID, Mode: hostapi.ModePerRun, ImageDigest: p.ImageDigest,
			VCPU: p.Resources.VCPU(), MemMiB: p.Resources.MemMiB(), RuntimeVersion: a.cfg.RuntimeVersion}
		min := p.MinWarm
		if min <= 0 {
			min = 2
		}
		out[poolKey{p.DeploymentID, a.shape(spec)}] = poolTarget{min: min, spec: spec, mode: hostapi.ModePerRun}
	}
	if a.cfg.ReplicaSurge > 0 && !a.revoked.Load() && a.canSpareReplicas() {
		for _, dv := range ds.VMs {
			if !dv.Running() || dv.Mode != hostapi.ModeReplica {
				continue
			}
			spec := a.vmSpec(dv)
			spec.VMID, spec.VMIP, spec.Slots = "", "", 0
			if spec.Net != nil {
				spec.Net = &vmm.NetAttach{Gateway: netpol.Gateway}
			}
			k := poolKey{dv.DeploymentID, a.shape(spec)}
			if _, ok := out[k]; !ok {
				out[k] = poolTarget{min: a.cfg.ReplicaSurge, spec: spec, mode: hostapi.ModeReplica}
			}
		}
	}
	return out
}

// canSpareReplicas: a replica spare needs its host link before it has a
// vm_ip, which only a link-owning backend (jailer + netns) or the process
// backend (no network) can do.
func (a *Agent) canSpareReplicas() bool {
	if a.cfg.Manager.Name() == "process" {
		return true
	}
	owner, ok := a.cfg.Manager.(vmm.LinkOwner)
	return ok && owner.OwnsReplicaLink()
}

// shape fills the host-known key parts so pool keys and store keys agree.
func (a *Agent) shape(s vmm.Spec) vmm.Shape {
	sh := s.Shape()
	if snap, ok := a.cfg.Manager.FindSnapshot(sh); ok {
		return snap.Shape
	}
	if kd, ok := a.cfg.Manager.(interface{ KernelDigest() string }); ok && sh.KernelDigest == "" {
		sh.KernelDigest = kd.KernelDigest()
	} else if sh.KernelDigest == "" && a.cfg.Manager.Name() == "process" {
		sh.KernelDigest = "process"
	}
	if sh.RuntimeVersion == "" {
		sh.RuntimeVersion = a.cfg.RuntimeVersion
	}
	return sh
}

// fillPools starts missing warm VMs and stops ones no target wants. Called
// from Reconcile (the only goroutine that starts or stops VMs; the fills
// themselves run concurrently and register their result).
func (a *Agent) fillPools(ctx context.Context, ds *hostapi.DesiredState, live bool) {
	targets := a.poolTargets(ds)
	a.mu.Lock()
	have := map[poolKey]int{}
	var drop []*warmVM
	for _, w := range a.warm {
		if _, ok := targets[w.key]; !ok && live {
			drop = append(drop, w)
			continue
		}
		have[w.key]++
	}
	for k, n := range a.filling {
		have[k] += n
	}
	total := len(a.vms) + len(a.warm)
	for _, n := range a.filling {
		total += n
	}
	var starts []poolKey
	now := time.Now()
	for k, t := range targets {
		if now.Before(a.poolBackoff[k]) {
			continue
		}
		for have[k] < t.min && total < a.cfg.MaxVMs {
			// Until the shape has a snapshot, one fill at a time: the first
			// cold boot produces the snapshot the others restore from.
			if _, ok := a.cfg.Manager.FindSnapshot(k.Shape); !ok && a.filling[k] > 0 {
				break
			}
			a.filling[k]++
			have[k]++
			total++
			starts = append(starts, k)
		}
	}
	a.mu.Unlock()
	for _, w := range drop {
		a.dropWarm(ctx, w, "no pool wants this shape")
	}
	for _, k := range starts {
		go a.provisionWarm(ctx, k, targets[k])
	}
}

// provisionWarm restores (or cold boots, then snapshots) one warm VM.
func (a *Agent) provisionWarm(ctx context.Context, k poolKey, t poolTarget) {
	defer func() {
		a.mu.Lock()
		a.filling[k]--
		if a.filling[k] <= 0 {
			delete(a.filling, k)
		}
		a.mu.Unlock()
		a.Kick()
	}()
	spec := t.spec
	spec.VMID, spec.Warm = newSlot(), true
	w := &warmVM{slot: spec.VMID, key: k, since: time.Now()}
	if spec.Net != nil {
		w.tap = netpol.TapName(spec.VMID)
		spec.Net.Tap, spec.Net.MAC = w.tap, netpol.MACFor("10.255.255.255") // placeholder; the claim sets the real MAC
	}
	vm, kind, err := a.boot(ctx, spec)
	if err != nil {
		a.poolFailed(k, "warm_"+kind+"_failed: "+err.Error())
		return
	}
	w.vm = vm
	if !a.waitControl(ctx, vm, a.cfg.BootGrace) {
		_ = a.cfg.Manager.Stop(ctx, vm)
		a.poolFailed(k, "warm VM answered no control ping within the boot grace")
		return
	}
	w.ready = true
	a.mu.Lock()
	a.warm[w.slot] = w
	depth := a.depth(k)
	a.mu.Unlock()
	a.event(w.slot, "warm_ready", "", "", map[string]string{"deployment": k.DeploymentID, "mode": t.mode,
		"restored": fmt.Sprint(vm.Restored), "depth": fmt.Sprint(depth)})
}

// boot starts a VM for spec: restore when the shape has a snapshot, else a
// cold boot that also takes the shape's first snapshot (if still clean).
// Under a link-owning backend the replica link exists only after Create, so
// the network policy is applied afterwards; otherwise before.
func (a *Agent) boot(ctx context.Context, spec vmm.Spec) (*vmm.VM, string, error) {
	spec.RuntimeVersion = a.cfg.RuntimeVersion
	start := time.Now()
	if snap, ok := a.cfg.Manager.FindSnapshot(a.shape(spec)); ok {
		vm, err := a.cfg.Manager.Restore(ctx, spec, snap)
		if err != nil {
			return nil, "restore", err
		}
		a.timing(spec.VMID, "restore", start)
		return vm, "restore", nil
	}
	vm, err := a.cfg.Manager.Create(ctx, spec)
	if err != nil {
		return nil, "create", err
	}
	if !a.waitControl(ctx, vm, a.cfg.BootGrace) {
		_ = a.cfg.Manager.Stop(ctx, vm)
		return nil, "create", errors.New("no control ping within the boot grace")
	}
	a.timing(spec.VMID, "cold_boot", start)
	// First VM of this shape on this host: snapshot it at "runtime up,
	// waiting for identity" (one at a time per shape).
	sh := a.shape(spec)
	a.mu.Lock()
	taking := a.snapshotting[sh]
	if !taking {
		a.snapshotting[sh] = true
	}
	a.mu.Unlock()
	if !taking {
		if _, ok := a.cfg.Manager.FindSnapshot(sh); !ok {
			ss := time.Now()
			ref, err := a.cfg.Manager.Snapshot(ctx, vm)
			if err != nil {
				a.logf("snapshot of %s (%s): %v", spec.VMID, sh.Key(), err)
				a.event(spec.VMID, "snapshot_failed", "", "", map[string]string{"reason": err.Error()})
			} else {
				a.timing(spec.VMID, "snapshot", ss)
				a.event(spec.VMID, "snapshot", "", "", map[string]string{"key": ref.Key, "image": ref.Shape.ImageDigest,
					"clean_attested": fmt.Sprint(ref.CleanAttested), "mem_bytes": fmt.Sprint(ref.MemBytes)})
			}
		}
		a.mu.Lock()
		delete(a.snapshotting, sh)
		a.mu.Unlock()
	}
	return vm, "create", nil
}

// waitControl polls the control channel until the guest answers a ping (not
// a bootstrap: the VM stays clean).
func (a *Agent) waitControl(ctx context.Context, vm *vmm.VM, max time.Duration) bool {
	deadline := time.Now().Add(max)
	for time.Now().Before(deadline) && ctx.Err() == nil {
		select {
		case <-vm.Exited:
			return false
		default:
		}
		step := time.Now().Add(200 * time.Millisecond)
		if c, err := a.cfg.Manager.DialControl(vm, step); err == nil {
			resp, err := guest.Exchange(c, guest.Request{Op: "ping"}, step)
			c.Close()
			if err == nil && resp.OK {
				return true
			}
		}
		time.Sleep(20 * time.Millisecond)
	}
	return false
}

func (a *Agent) poolFailed(k poolKey, reason string) {
	a.logf("warm pool %s: %s", k.DeploymentID, reason)
	a.mu.Lock()
	a.poolBackoff[k] = time.Now().Add(5 * time.Second)
	a.mu.Unlock()
	a.event("", "warm_failed", "", "", map[string]string{"deployment": k.DeploymentID, "reason": reason})
}

func (a *Agent) depth(k poolKey) int {
	n := 0
	for _, w := range a.warm {
		if w.key == k && w.ready {
			n++
		}
	}
	return n
}

// takeWarm removes and returns a ready warm VM for the key, or nil.
func (a *Agent) takeWarm(k poolKey) *warmVM {
	a.mu.Lock()
	defer a.mu.Unlock()
	var best *warmVM
	for _, w := range a.warm {
		if w.key == k && w.ready && (best == nil || w.since.Before(best.since)) {
			best = w
		}
	}
	if best != nil {
		delete(a.warm, best.slot)
	}
	return best
}

func (a *Agent) dropWarm(ctx context.Context, w *warmVM, why string) {
	a.mu.Lock()
	delete(a.warm, w.slot)
	a.mu.Unlock()
	if w.vm != nil {
		_ = a.cfg.Manager.Stop(ctx, w.vm)
	}
	if w.tap != "" {
		_ = a.cfg.Net.Remove(ctx, w.tap)
	}
	a.event(w.slot, "warm_dropped", "", "", map[string]string{"why": why})
}

// warmPoolsLocked is the heartbeat's warm_pools block: depth per deployment.
func (a *Agent) warmPoolsLocked(ds *hostapi.DesiredState) map[string]hostapi.PoolDepth {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.warmPools(ds)
}

func (a *Agent) warmPools(ds *hostapi.DesiredState) map[string]hostapi.PoolDepth {
	pools := map[string]hostapi.PoolDepth{}
	for k, t := range a.poolTargets(ds) {
		p := pools[k.DeploymentID]
		p.MinWarm += t.min
		pools[k.DeploymentID] = p
	}
	if ds != nil {
		for _, p := range ds.Pools { // frozen pools still appear, at depth 0
			if _, ok := pools[p.DeploymentID]; !ok {
				min := p.MinWarm
				if min <= 0 {
					min = 2
				}
				pools[p.DeploymentID] = hostapi.PoolDepth{MinWarm: min}
			}
		}
	}
	for _, w := range a.warm {
		if w.ready {
			p := pools[w.key.DeploymentID]
			p.Depth++
			pools[w.key.DeploymentID] = p
		}
	}
	return pools
}

// WarmDepth is the ready warm VMs for a deployment (tests).
func (a *Agent) WarmDepth(deploymentID string) int {
	a.mu.Lock()
	defer a.mu.Unlock()
	n := 0
	for _, w := range a.warm {
		if w.ready && w.key.DeploymentID == deploymentID {
			n++
		}
	}
	return n
}

// timing emits a detail-only event with a measured duration.
func (a *Agent) timing(vmID, kind string, start time.Time) {
	a.event(vmID, "vm_timing", "", "", map[string]string{"kind": kind, "ms": fmt.Sprint(time.Since(start).Milliseconds())})
}

// gcSnapshots deletes snapshots whose image digest no desired VM or pool
// references any more, once they are older than SnapshotGCGrace.
func (a *Agent) gcSnapshots(ds *hostapi.DesiredState) {
	if ds == nil {
		return
	}
	a.mu.Lock()
	due := time.Since(a.lastGC) >= a.cfg.SnapshotGCEvery
	if due {
		a.lastGC = time.Now()
	}
	a.mu.Unlock()
	if !due {
		return
	}
	referenced := map[string]bool{}
	for _, dv := range ds.VMs {
		if dv.Running() {
			referenced[dv.ImageDigest] = true
		}
	}
	for _, p := range ds.Pools {
		referenced[p.ImageDigest] = true
	}
	for _, ref := range a.cfg.Manager.Snapshots() {
		if referenced[ref.Shape.ImageDigest] || ref.Age(time.Now()) < a.cfg.SnapshotGCGrace {
			continue
		}
		if err := a.cfg.Manager.DeleteSnapshot(ref); err != nil {
			a.logf("snapshot gc %s: %v", ref.Key, err)
			continue
		}
		a.event("", "snapshot_gc", "", "", map[string]string{"key": ref.Key, "image": ref.Shape.ImageDigest})
	}
}
