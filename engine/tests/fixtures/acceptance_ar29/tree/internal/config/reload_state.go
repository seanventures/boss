package config

import "sync"

// ReloadState tracks the tri-state readiness the PRD requires for /readyz:
// never loaded, serving the last good config after a rejected reload, or
// serving the current (just-loaded) config.
type ReloadState struct {
	mu            sync.Mutex
	everLoaded    bool
	lastRejectReason string
}

func (r *ReloadState) MarkLoaded() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.everLoaded = true
	r.lastRejectReason = ""
}

func (r *ReloadState) MarkRejected(reason string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.lastRejectReason = reason
}

// Ready reports whether /readyz should answer true: false only if no
// config has ever loaded successfully.
func (r *ReloadState) Ready() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.everLoaded
}

func (r *ReloadState) LastRejectReason() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.lastRejectReason
}
