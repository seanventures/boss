// Package job is the in-process typed job + lease store for the S1 scaffold.
// It does not boot Firecracker. Expired leases become timeout — never a silent drop.
package job

import (
	"fmt"
	"os"
	"sync"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/contract"
)

const DefaultLeaseTTL = 120 * time.Second

// DefaultRetention keeps a terminal task's workspace (the retained rootfs /
// workdir) around for inspection before Release drops it.
const DefaultRetention = 2 * time.Hour

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
	// Executing marks a job whose backend run is in flight server-side; the
	// lease reaper must not expire it (the engine polls, it does not heartbeat).
	Executing bool
	// TaskDir is the per-task workspace on disk (spec/result/logs/retained
	// workdir); dropped when the job is released.
	TaskDir string
}

type Lease struct {
	TTL         time.Duration
	HeartbeatAt time.Time
	ExpiresAt   time.Time
}

type Store struct {
	mu              sync.Mutex
	jobs            map[string]*Job
	now             func() time.Time
	ttl             time.Duration
	claimsFrozen    bool
	firecrackerLive bool
	seq             int
	backendName     string
	backendCheck    func() error
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

// ConfigureBackend registers a live execution backend (process or firecracker).
// `check` is the readiness gate: nil error = submits may be admitted. Without
// this call the store keeps the original fail-closed scaffold behavior.
func (s *Store) ConfigureBackend(name string, check func() error) {
	s.backendName = name
	s.backendCheck = check
	s.firecrackerLive = name == "firecracker" && check != nil && check() == nil
}

func (s *Store) BackendName() string {
	if s.backendName == "" {
		return "none"
	}
	return s.backendName
}

// BackendLive reports whether a backend is configured AND currently ready.
func (s *Store) BackendLive() bool {
	return s.backendCheck != nil && s.backendCheck() == nil
}

// BackendError returns the readiness failure naming the missing piece ("" when
// ready or when no backend is configured at all).
func (s *Store) BackendError() string {
	if s.backendCheck == nil {
		return ""
	}
	if err := s.backendCheck(); err != nil {
		return err.Error()
	}
	return ""
}

func (s *Store) FirecrackerLive() bool {
	// Live fact about the substrate, refreshed on ask — never invented.
	return s.backendName == "firecracker" && s.BackendLive()
}

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
	if s.backendCheck == nil {
		return contract.ReasonFirecrackerDisabled,
			"Firecracker backend is not enabled; this in-repo S1 scaffold does not run live microVMs"
	}
	if err := s.backendCheck(); err != nil {
		// Backend configured but a piece is missing — name it, never guess.
		if s.backendName == "firecracker" {
			return contract.ReasonFirecrackerDisabled, err.Error()
		}
		return contract.ReasonBackendUnavailable, err.Error()
	}
	return "", ""
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

// CreateQueued registers a real (backend-executed) job. The runner takes it
// from queued through running to a terminal status; while Executing the lease
// reaper leaves it alone (the engine polls, it does not heartbeat).
func (s *Store) CreateQueued(req contract.SubmitRequest, taskDir string) *Job {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	s.seq++
	id := fmt.Sprintf("sbx_%d_%d", now.Unix(), s.seq)
	job := &Job{
		ID:        id,
		TaskID:    req.TaskID,
		TenantID:  req.TenantID,
		Status:    contract.StatusQueued,
		Phase:     "provisioning",
		CreatedAt: now,
		Lease: Lease{
			TTL:         s.ttl,
			HeartbeatAt: now,
			ExpiresAt:   now.Add(s.ttl),
		},
		Engine:    req.Engine,
		TestMode:  req.TestMode,
		Executing: true,
		TaskDir:   taskDir,
	}
	s.jobs[id] = job
	return cloneJob(job)
}

// MarkRunning flips a queued job to running with the given phase.
func (s *Store) MarkRunning(id, phase string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if job, ok := s.jobs[id]; ok && !contract.IsTerminal(job.Status) {
		job.Status = contract.StatusRunning
		job.Phase = phase
		job.Lease.HeartbeatAt = s.now()
	}
}

// finishLocked applies a terminal status and starts the retention window.
func (s *Store) finishLocked(job *Job, status, phase, reason, errMsg string, result *contract.Result) {
	if contract.IsTerminal(job.Status) {
		return // cancel/timeout already landed — never overwrite a terminal truth
	}
	now := s.now()
	job.Status = status
	job.Phase = phase
	job.Reason = reason
	job.Error = errMsg
	job.Result = result
	job.Executing = false
	job.Lease.HeartbeatAt = now
	job.Lease.ExpiresAt = now.Add(DefaultRetention)
}

func (s *Store) Complete(id string, result *contract.Result) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if job, ok := s.jobs[id]; ok {
		s.finishLocked(job, contract.StatusDone, "done", "", "", result)
	}
}

func (s *Store) Fail(id, errMsg string, result *contract.Result) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if job, ok := s.jobs[id]; ok {
		if result == nil {
			result = &contract.Result{Status: "error", Error: errMsg}
		}
		s.finishLocked(job, contract.StatusFailed, "failed", "", errMsg, result)
	}
}

func (s *Store) MarkTimeout(id, errMsg string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if job, ok := s.jobs[id]; ok {
		s.finishLocked(job, contract.StatusTimeout, "timeout", "", errMsg,
			&contract.Result{Status: "error", Error: errMsg})
	}
}

func (s *Store) MarkCancelled(id, reason string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if job, ok := s.jobs[id]; ok {
		if reason == "" {
			reason = contract.ReasonCancelled
		}
		s.finishLocked(job, contract.StatusCancelled, "cancelled", contract.ReasonCancelled,
			"cancelled: "+reason, &contract.Result{Status: "error", Error: "cancelled: " + reason})
	}
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
	if now.After(job.Lease.ExpiresAt) && !job.HeldUntil.After(now) && !job.Executing {
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
	if job.TaskDir != "" {
		_ = os.RemoveAll(job.TaskDir) // free the workspace with the slot
	}
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
		if contract.IsTerminal(job.Status) || job.Released || job.Executing {
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
