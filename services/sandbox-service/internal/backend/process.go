package backend

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
)

// Process runs the in-guest pipeline as a local subprocess in the task dir.
// No isolation claim — it exists so the contract (submit → status → result →
// release) is exercisable end to end on any machine with python3 + git.
type Process struct {
	// Python is the interpreter to run the guest agent with (default python3).
	Python string
	// AgentPath optionally points at an on-disk in_guest_agent.py; empty means
	// the embedded copy is written into the task dir and used.
	AgentPath string
}

func (p *Process) Name() string { return "process" }

func (p *Process) python() string {
	if p.Python != "" {
		return p.Python
	}
	return "python3"
}

func (p *Process) Check() error {
	if _, err := exec.LookPath(p.python()); err != nil {
		return fmt.Errorf("process backend: %s not found on PATH", p.python())
	}
	if _, err := exec.LookPath("git"); err != nil {
		return fmt.Errorf("process backend: git not found on PATH")
	}
	if p.AgentPath != "" {
		if _, err := os.Stat(p.AgentPath); err != nil {
			return fmt.Errorf("process backend: guest agent missing at %s", p.AgentPath)
		}
	}
	return nil
}

func (p *Process) Run(ctx context.Context, r RunRequest) error {
	if err := p.Check(); err != nil {
		return err
	}
	agent := p.AgentPath
	if agent == "" {
		agent = filepath.Join(r.TaskDir, "in_guest_agent.py")
		if err := os.WriteFile(agent, EmbeddedGuestAgent, 0o644); err != nil {
			return fmt.Errorf("process backend: write guest agent: %w", err)
		}
	}
	workDir := filepath.Join(r.TaskDir, "work")
	if err := os.MkdirAll(workDir, 0o755); err != nil {
		return fmt.Errorf("process backend: mkdir work: %w", err)
	}

	runCtx := ctx
	if r.Timeout > 0 {
		var cancel context.CancelFunc
		runCtx, cancel = context.WithTimeout(ctx, r.Timeout)
		defer cancel()
	}
	cmd := exec.CommandContext(runCtx, p.python(), agent)
	cmd.Dir = r.TaskDir
	cmd.Env = append(os.Environ(),
		"BOSS_SPEC_PATH="+r.SpecPath,
		"BOSS_RESULT_PATH="+r.ResultPath,
		"BOSS_WORKDIR="+filepath.Join(workDir, "repo"),
	)
	console, err := os.Create(r.ConsoleLog)
	if err != nil {
		return fmt.Errorf("process backend: open console log: %w", err)
	}
	defer console.Close()
	cmd.Stdout = console
	cmd.Stderr = console

	runErr := cmd.Run()
	if runCtx.Err() != nil {
		return runCtx.Err() // timeout / cancel — the runner maps these to status
	}
	if runErr != nil {
		// The guest agent writes result.json {"status":"error"} on its own
		// failures; a non-zero exit without a result is an infra failure.
		if _, statErr := os.Stat(r.ResultPath); statErr != nil {
			return fmt.Errorf("process backend: guest agent exited: %v (no result.json)", runErr)
		}
	}
	return nil
}
