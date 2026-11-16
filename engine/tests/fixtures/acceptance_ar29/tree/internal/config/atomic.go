package config

import "sync/atomic"

// Store holds the currently-active, immutable config Snapshot. Readers call
// Current(); the reload loop calls Swap() after validating a new snapshot.
// Both are safe for concurrent use.
type Store struct {
	ptr atomic.Pointer[Snapshot]
}

// Swap atomically replaces the active snapshot.
func (s *Store) Swap(snap *Snapshot) {
	s.ptr.Store(snap)
}

// Current returns the active snapshot, or nil if none has ever been set.
func (s *Store) Current() *Snapshot {
	return s.ptr.Load()
}
