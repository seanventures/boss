package hostd

import (
	"context"
	"sync"
	"time"

	"github.com/seanventures/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanventures/boss/services/sandbox-service/internal/vmm"
)

// vmState is the agent's in-memory view of one VM.
type vmState struct {
	mu             sync.Mutex
	id             string
	spec           hostapi.DesiredVM
	haveSpec       bool
	vm             *vmm.VM
	tap            string
	state          string
	reason         string
	startedAt      time.Time
	lastPing       time.Time
	misses         int
	bootstrapped   bool
	restarts       []time.Time
	crashloopUntil time.Time
	drops          uint64
	stopWatch      context.CancelFunc
	crashloops     int       // crashloops so far; a failure after one quarantines
	servingSince   time.Time // first answered ping of this incarnation
	busySlots      int       // from the 500 ms ping (local use only)
	totalSlots     int
	pending        int       // deliveries since the last ping
	claimStart     time.Time // startVM entry; cleared by the first bootstrap (timing)
}

// freeSlots is what the host may still deliver to this VM (local only:
// BOSS keeps its own count). 0 unless serving.
func (v *vmState) freeSlots() int {
	if v.state != hostapi.StateServing {
		return 0
	}
	f := v.totalSlots - v.busySlots - v.pending
	if f < 0 {
		return 0
	}
	return f
}

func (v *vmState) setState(state, reason string) (prev string) {
	v.mu.Lock()
	defer v.mu.Unlock()
	prev = v.state
	v.state, v.reason = state, reason
	return prev
}

func (v *vmState) getState() string {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.state
}

func (v *vmState) setDrops(n uint64) {
	v.mu.Lock()
	v.drops = n
	v.mu.Unlock()
}

// refusal is a desired VM the host refused, with its named reason.
type refusal struct {
	replicaID string
	reason    string
}

func (v *vmState) status() hostapi.ReplicaStatus {
	v.mu.Lock()
	defer v.mu.Unlock()
	age := int64(-1)
	if !v.lastPing.IsZero() {
		age = time.Since(v.lastPing).Milliseconds()
	}
	slots := v.spec.Slots
	if slots == 0 && v.spec.Mode == hostapi.ModeReplica {
		slots = 4
	} else if slots == 0 {
		slots = 1
	}
	return hostapi.ReplicaStatus{
		ReplicaID: v.spec.ReplicaID, VMID: v.id, DeploymentID: v.spec.DeploymentID, Mode: v.spec.Mode,
		State: v.state, Reason: v.reason, VMIP: v.spec.VMIP, ImageDigest: v.spec.ImageDigest, Slots: slots, Busy: v.busySlots,
		VsockPingAgeMS: age, Restarts: len(v.restarts), DropsOriginated: v.drops, FreeSlots: v.freeSlots(),
	}
}

func (v *vmState) cancelWatch() {
	v.mu.Lock()
	cancel := v.stopWatch
	v.stopWatch = nil
	v.mu.Unlock()
	if cancel != nil {
		cancel()
	}
}
