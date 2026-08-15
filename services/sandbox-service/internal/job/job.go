// Package job is the in-process typed job + lease store for the S1 scaffold.
// It does not boot Firecracker. Expired leases become timeout — never a silent drop.
package job

import (
	"fmt"
	"sync"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/contract"
)

const DefaultLeaseTTL = 120 * time.Second

type Job struct {
	ID        string
	TaskID    string
	TenantID  string
	Status    string
	Phase     string
	Reason    string
	Error     string
	Result    *contract.Result
	CreatedAt time.Time
	Lease     Lease
	HeldUntil time.Time
	Released  bool
	Engine    string
	TestMode  bool
}

type Lease struct {
	TTL         time.Duration
	HeartbeatAt time.Time
	ExpiresAt   time.Time
}

type Store struct {
	mu               sync.Mutex
	jobs             map[string]*Job
	now              func() time.Time
	ttl              time.Duration
	claimsFrozen     bool
	firecrackerLive  bool
	seq              int
}

func NewStore(claimsFrozen bool) *Store {
	return &Store{
		jobs:            map[string]*Job{},
		now:             time.Now,
		ttl:             DefaultLeaseTTL,
		claimsFrozen:    claimsFrozen,
		firecrackerLive: false, // this scaffold never enables live Firecracker
	}
}

func (s *Store) SetClock(now func() time.Time) { s.now = now }
func (s *Store) SetTTL(d time.Duration)        { s.ttl = d }
func (s *Store) ClaimsFrozen() bool            { return s.claimsFrozen }
func (s *Store) FirecrackerLive() bool         { return s.firecrackerLive }

// Admit decides whether a submit may create work. Fail-closed with a named reason.
func (s *Store) Admit(req contract.SubmitRequest) (reason, message string) {
	if problems := contract.ValidateSubmit(req); len(problems) > 0 {
		return contract.ReasonContractInvalid, problems[0]
	}
	if contract.IsEcho(req) {
		return "", ""
	}
	if s.claimsFrozen {
		return contract.ReasonClaimsFrozen,
			"microVM execution is not claimed for Community GA (BOSS_MICROVM_CLAIMS_FROZEN=true)"
	}
	if !s.firecrackerLive {
		return contract.ReasonFirecrackerDisabled,
			"Firecracker backend is not enabled; this in-repo S1 scaffold does not run live microVMs"
	}
	return contract.ReasonBackendUnavailable,
		"sandbox backend unavailable"
}

func (s *Store) CreateEcho(req contract.SubmitRequest) *Job {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	s.seq++
	id := fmt.Sprintf("sbx_s1_%d", s.seq)
	job := &Job{
		ID:        id,
		TaskID:    req.TaskID,
		TenantID:  req.TenantID,
		Status:    contract.StatusDone,
		Phase:     "echo",
		Reason:    "",
		CreatedAt: now,
		Lease: Lease{
			TTL:         s.ttl,
			HeartbeatAt: now,
			ExpiresAt:   now.Add(s.ttl),
		},
		Engine:   req.Engine,
		TestMode: req.TestMode,
		Result: &contract.Result{
			Diff: []contract.FileChange{
				{Path: "SANDBOX_OK.txt", Content: "ok"},
			},
			TestsPassed: true,
			Summary:     "echo engine wrote SANDBOX_OK.txt (stub; firecracker_live=false)",
		},
	}
	s.jobs[id] = job
	return cloneJob(job)
}

func (s *Store) Get(id string) (*Job, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.reapLocked()
	job, ok := s.jobs[id]
	if !ok {
		return nil, false
	}
	return cloneJob(job), true
}

func (s *Store) Heartbeat(id string) (*Job, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.reapLocked()
	job, ok := s.jobs[id]
	if !ok || job.Released {
		return nil, false
	}
	now := s.now()
	if now.After(job.Lease.ExpiresAt) && !job.HeldUntil.After(now) {
		s.expireLocked(job, now)
		return cloneJob(job), true
	}
	job.Lease.HeartbeatAt = now
	job.Lease.ExpiresAt = now.Add(job.Lease.TTL)
	return cloneJob(job), true
}

func (s *Store) Release(id string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	job, ok := s.jobs[id]
	if !ok {
		return false
	}
	// Success-release: drop the job. Failures are retained for inspection.
	if job.Status == contract.StatusFailed || job.Status == contract.StatusTimeout {
		return true
	}
	job.Released = true
	delete(s.jobs, id)
	return true
}

func (s *Store) Hold(id string, ttl time.Duration) (*Job, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	job, ok := s.jobs[id]
	if !ok {
		return nil, false
	}
	now := s.now()
	if ttl <= 0 {
		ttl = s.ttl
	}
	job.HeldUntil = now.Add(ttl)
	job.Lease.ExpiresAt = job.HeldUntil
	job.Lease.HeartbeatAt = now
	return cloneJob(job), true
}

func (s *Store) RunningCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.reapLocked()
	n := 0
	for _, job := range s.jobs {
		if !contract.IsTerminal(job.Status) {
			n++
		}
	}
	return n
}

func (s *Store) reapLocked() {
	now := s.now()
	for _, job := range s.jobs {
		if contract.IsTerminal(job.Status) || job.Released {
			continue
		}
		if job.HeldUntil.After(now) {
			continue
		}
		if !now.After(job.Lease.ExpiresAt) {
			continue
		}
		s.expireLocked(job, now)
	}
}

func (s *Store) expireLocked(job *Job, now time.Time) {
	job.Status = contract.StatusTimeout
	job.Phase = "lease"
	job.Reason = contract.ReasonLeaseExpired
	job.Error = "lease expired without heartbeat — fail visible, not dropped"
	job.Result = &contract.Result{
		Diff:        nil,
		TestsPassed: false,
		Summary:     "",
		Error:       contract.ReasonLeaseExpired,
	}
	_ = now
}

func cloneJob(job *Job) *Job {
	cp := *job
	if job.Result != nil {
		res := *job.Result
		if job.Result.Diff != nil {
			res.Diff = append([]contract.FileChange(nil), job.Result.Diff...)
		}
		cp.Result = &res
	}
	return &cp
}

func (j *Job) LeaseView() *contract.LeaseView {
	return &contract.LeaseView{
		TTLSeconds:  int(j.Lease.TTL / time.Second),
		HeartbeatAt: j.Lease.HeartbeatAt.UTC().Format(time.RFC3339),
		ExpiresAt:   j.Lease.ExpiresAt.UTC().Format(time.RFC3339),
		Expired:     j.Status == contract.StatusTimeout && j.Reason == contract.ReasonLeaseExpired,
	}
}
