// Package run drives one admitted submit through a backend: write the flat
// guest spec, execute, parse the guest's result.json, and land a terminal
// status in the job store. Failures are always visible (status + error),
// never a silent drop or an invented success.
package run

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/backend"
	"github.com/seanheiney/boss/services/sandbox-service/internal/contract"
	"github.com/seanheiney/boss/services/sandbox-service/internal/job"
)

// DefaultTaskTimeout bounds a task when the submit body carries no timeout.
const DefaultTaskTimeout = 900 * time.Second

type Runner struct {
	Store    *job.Store
	Backend  backend.Backend
	WorkRoot string
	// MaxTimeout caps what a submit body may ask for (0 = no cap).
	MaxTimeout time.Duration

	mu      sync.Mutex
	cancels map[string]context.CancelFunc
}

func New(store *job.Store, b backend.Backend, workRoot string) *Runner {
	return &Runner{Store: store, Backend: b, WorkRoot: workRoot,
		cancels: map[string]context.CancelFunc{}}
}

// Submit admits-and-starts one task; the caller has already run store.Admit.
// It returns the created job (status queued) and executes asynchronously.
func (r *Runner) Submit(req contract.SubmitRequest) (*job.Job, error) {
	taskDir, err := os.MkdirTemp(r.WorkRoot, "task-")
	if err != nil {
		return nil, fmt.Errorf("create task workspace: %w", err)
	}
	spec := contract.GuestSpec(req)
	raw, err := json.Marshal(spec)
	if err != nil {
		_ = os.RemoveAll(taskDir)
		return nil, fmt.Errorf("encode guest spec: %w", err)
	}
	specPath := filepath.Join(taskDir, "spec.json")
	if err := os.WriteFile(specPath, raw, 0o600); err != nil {
		_ = os.RemoveAll(taskDir)
		return nil, fmt.Errorf("write guest spec: %w", err)
	}
	created := r.Store.CreateQueued(req, taskDir)

	timeout := DefaultTaskTimeout
	if t := contract.TimeoutSeconds(req.Timeout); t > 0 {
		timeout = time.Duration(t) * time.Second
	}
	if r.MaxTimeout > 0 && timeout > r.MaxTimeout {
		timeout = r.MaxTimeout
	}

	ctx, cancel := context.WithCancel(context.Background())
	r.mu.Lock()
	r.cancels[created.ID] = cancel
	r.mu.Unlock()

	go r.execute(ctx, created.ID, backend.RunRequest{
		TaskID:     req.TaskID,
		TaskDir:    taskDir,
		SpecPath:   specPath,
		ResultPath: filepath.Join(taskDir, "result.json"),
		ConsoleLog: filepath.Join(taskDir, "console.log"),
		Timeout:    timeout,
	})
	return created, nil
}

// Cancel stops a running task; returns false when the id is unknown.
func (r *Runner) Cancel(id, reason string) bool {
	if _, ok := r.Store.Get(id); !ok {
		return false
	}
	r.Store.MarkCancelled(id, reason)
	r.mu.Lock()
	cancel := r.cancels[id]
	r.mu.Unlock()
	if cancel != nil {
		cancel()
	}
	return true
}

func (r *Runner) execute(ctx context.Context, id string, req backend.RunRequest) {
	defer func() {
		r.mu.Lock()
		delete(r.cancels, id)
		r.mu.Unlock()
		if rec := recover(); rec != nil {
			r.Store.Fail(id, fmt.Sprintf("sandbox runner panicked: %v", rec), nil)
		}
	}()
	r.Store.MarkRunning(id, "provisioning")
	r.Store.MarkRunning(id, "executing")
	err := r.Backend.Run(ctx, req)
	switch {
	case ctx.Err() == context.Canceled:
		// MarkCancelled already landed the terminal status.
		return
	case errors.Is(err, context.DeadlineExceeded):
		r.Store.MarkTimeout(id, fmt.Sprintf("sandbox task exceeded its %s budget", req.Timeout))
		return
	case err != nil:
		r.Store.Fail(id, err.Error(), nil)
		return
	}
	result, perr := parseGuestResult(req.ResultPath)
	if perr != nil {
		r.Store.Fail(id, perr.Error(), nil)
		return
	}
	if result.Status != "" && result.Status != "ok" {
		msg := result.Error
		if msg == "" {
			msg = "guest agent reported failure"
		}
		r.Store.Fail(id, msg, result)
		return
	}
	r.Store.Complete(id, result)
}

// parseGuestResult reads the guest's /work/result.json defensively: a
// malformed result degrades to a failed run, never a crash.
func parseGuestResult(path string) (*contract.Result, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("guest produced no result.json: %v", err)
	}
	var result contract.Result
	if err := json.Unmarshal(raw, &result); err != nil {
		return nil, fmt.Errorf("guest result.json is not valid JSON: %v", err)
	}
	return &result, nil
}
