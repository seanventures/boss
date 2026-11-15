package hostd

import (
	"context"
	"sync"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanheiney/boss/services/sandbox-service/internal/vmm"
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
		State: v.state, Reason: v.reason, VMIP: v.spec.VMIP, ImageDigest: v.spec.ImageDigest, Slots: slots, Busy: 0,
		VsockPingAgeMS: age, Restarts: len(v.restarts), DropsOriginated: v.drops,
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
