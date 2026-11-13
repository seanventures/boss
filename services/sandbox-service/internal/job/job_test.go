package job

import (
	"testing"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/contract"
)

func TestAdmitFailsClosedWhenFrozen(t *testing.T) {
	s := NewStore(true)
	req := contract.SubmitRequest{
		ContractVersion: contract.Version,
		TaskID:          "crun_x",
		Repo:            contract.RepoSpec{Owner: "o", Name: "r"},
		Engine:          "claude_code",
		EngineEnv:       map[string]string{},
	}
	reason, msg := s.Admit(req)
	if reason != contract.ReasonClaimsFrozen {
		t.Fatalf("reason=%q want %s", reason, contract.ReasonClaimsFrozen)
	}
	if msg == "" {
		t.Fatal("expected named message")
	}
}

func TestAdmitFailsClosedWhenUnfrozenButNoFirecracker(t *testing.T) {
	s := NewStore(false)
	req := contract.SubmitRequest{
		ContractVersion: contract.Version,
		TaskID:          "crun_x",
		Repo:            contract.RepoSpec{Owner: "o", Name: "r"},
		Engine:          "openai",
		EngineEnv:       map[string]string{},
	}
	reason, _ := s.Admit(req)
	if reason != contract.ReasonFirecrackerDisabled {
		t.Fatalf("reason=%q want %s", reason, contract.ReasonFirecrackerDisabled)
	}
	if s.FirecrackerLive() {
		t.Fatal("scaffold must not report firecracker live")
	}
}

func TestHeartbeatExtendsLeaseAndExpiryIsVisible(t *testing.T) {
	s := NewStore(true)
	now := time.Date(2026, 8, 15, 12, 0, 0, 0, time.UTC)
	s.SetClock(func() time.Time { return now })
	s.SetTTL(30 * time.Second)

	job := s.CreateEcho(contract.SubmitRequest{
		ContractVersion: contract.Version,
		TaskID:          "exercise_echo_api",
		Engine:          "echo",
		TestMode:        true,
		EngineEnv:       map[string]string{},
	})
	if job.Lease.ExpiresAt.Sub(now) != 30*time.Second {
		t.Fatalf("ttl not applied: %v", job.Lease.ExpiresAt)
	}

	now = now.Add(10 * time.Second)
	got, ok := s.Heartbeat(job.ID)
	if !ok {
		t.Fatal("heartbeat missed")
	}
	if got.Lease.ExpiresAt.Sub(now) != 30*time.Second {
		t.Fatalf("heartbeat did not extend lease: %v vs %v", got.Lease.ExpiresAt, now)
	}
}

func TestExpiredLeaseBecomesTimeoutNotSilentDrop(t *testing.T) {
	s := NewStore(true)
	now := time.Date(2026, 8, 15, 12, 0, 0, 0, time.UTC)
	s.SetClock(func() time.Time { return now })
	s.SetTTL(5 * time.Second)

	// Force a non-terminal job so reap can expire it.
	job := s.CreateEcho(contract.SubmitRequest{
		ContractVersion: contract.Version,
		TaskID:          "exercise_echo_api",
		Engine:          "echo",
		TestMode:        true,
		EngineEnv:       map[string]string{},
	})
	s.mu.Lock()
	s.jobs[job.ID].Status = contract.StatusRunning
	s.jobs[job.ID].Phase = "implement"
	s.jobs[job.ID].Result = nil
	s.mu.Unlock()

	now = now.Add(6 * time.Second)
	got, ok := s.Get(job.ID)
	if !ok {
		t.Fatal("expired job was silently dropped")
	}
	if got.Status != contract.StatusTimeout {
		t.Fatalf("status=%q want timeout", got.Status)
	}
	if got.Reason != contract.ReasonLeaseExpired {
		t.Fatalf("reason=%q", got.Reason)
	}
}

func TestReleaseDropsSuccessRetainsFailure(t *testing.T) {
	s := NewStore(true)
	okJob := s.CreateEcho(contract.SubmitRequest{
		ContractVersion: contract.Version,
		TaskID:          "echo_ok",
		Engine:          "echo",
		TestMode:        true,
		EngineEnv:       map[string]string{},
	})
	if !s.Release(okJob.ID) {
		t.Fatal("release success")
	}
	if _, found := s.Get(okJob.ID); found {
		t.Fatal("successful job should be released")
	}

	fail := s.CreateEcho(contract.SubmitRequest{
		ContractVersion: contract.Version,
		TaskID:          "echo_fail",
		Engine:          "echo",
		TestMode:        true,
		EngineEnv:       map[string]string{},
	})
	s.mu.Lock()
	s.jobs[fail.ID].Status = contract.StatusFailed
	s.mu.Unlock()
	if !s.Release(fail.ID) {
		t.Fatal("release of failed job should no-op retain")
	}
	if _, found := s.Get(fail.ID); !found {
		t.Fatal("failed job must be retained")
	}
}
