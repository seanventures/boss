// Package backend holds the sandbox-service execution backends behind one
// interface. Both run the SAME in-guest pipeline (clone → agent → test → diff,
// sandbox-host/guest/in_guest_agent.py) and exchange spec.json / result.json:
//
//   - process:     a local workdir subprocess (no virtualization). Exists so the
//     Python ⇄ Go contract is testable on any machine and in CI.
//   - firecracker: one Firecracker microVM per task (kernel + rootfs + work
//     drive). The real isolation substrate; refuses cleanly when a piece
//     (/dev/kvm, binary, images) is missing.
//
// A backend never invents success: it either produces the guest's result.json
// or returns an error the job store records as a visible failure.
package backend

import (
	"context"
	_ "embed"
	"time"
)

// EmbeddedGuestAgent is the in-guest pipeline, embedded so the built binary is
// self-contained. The canonical source is sandbox-host/guest/in_guest_agent.py;
// a Go test asserts the two stay byte-identical.
//
//go:embed guest/in_guest_agent.py
var EmbeddedGuestAgent []byte

// RunRequest is one task execution. The caller (runner) prepares TaskDir with
// spec.json and parses ResultPath afterwards.
type RunRequest struct {
	TaskID     string
	TaskDir    string        // per-task scratch dir on the host
	SpecPath   string        // <TaskDir>/spec.json — the flat guest spec
	ResultPath string        // <TaskDir>/result.json — written by the guest
	ConsoleLog string        // <TaskDir>/console.log — serial console / agent stdio
	Timeout    time.Duration // wall-clock budget; the backend must stop at it
}

// Backend executes one task. Check() is the readiness gate: nil means a submit
// may be admitted; an error names exactly which piece is missing.
type Backend interface {
	Name() string
	Check() error
	Run(ctx context.Context, r RunRequest) error
}
