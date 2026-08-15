// Package contract is the Go side of the versioned Python ⇄ sandbox-service
// job API (docs/infra/PYTHON_GO_SANDBOX_CONTRACT.md). Python owns the
// envelope; this package conforms. Changes are additive.
package contract

import (
	"encoding/json"
	"fmt"
	"strings"
)

const Version = "1.0"

const (
	StatusQueued  = "queued"
	StatusRunning = "running"
	StatusDone    = "done"
	StatusFailed  = "failed"
	StatusTimeout = "timeout"
)

// Named fail-closed reasons. Never invent success; never drop these.
const (
	ReasonClaimsFrozen         = "microvm_claims_frozen"
	ReasonFirecrackerDisabled  = "firecracker_not_enabled"
	ReasonContractInvalid      = "contract_invalid"
	ReasonUnauthorized         = "unauthorized"
	ReasonTaskMissing          = "sandbox_task_missing"
	ReasonLeaseExpired         = "lease_expired"
	ReasonBackendUnavailable   = "sandbox_backend_unavailable"
)

// SubmitRequest is the typed POST /v1/sandbox/tasks body.
type SubmitRequest struct {
	ContractVersion     string            `json:"contract_version"`
	TaskID              string            `json:"task_id"`
	TenantID            string            `json:"tenant_id"`
	Repo                RepoSpec          `json:"repo"`
	GitToken            string            `json:"git_token"`
	Instructions        string            `json:"instructions"`
	AcceptanceCriteria  []string          `json:"acceptance_criteria"`
	TestCommands        []string          `json:"test_commands"`
	Engine              string            `json:"engine"`
	EngineEnv           map[string]string `json:"engine_env"`
	RunMutation         bool              `json:"run_mutation"`
	MaxTurns            *int              `json:"max_turns"`
	Timeout             any               `json:"timeout"`
	Resources           map[string]any    `json:"resources"`
	TestMode            bool              `json:"test_mode"`
}

type RepoSpec struct {
	Owner      string `json:"owner"`
	Name       string `json:"name"`
	BaseBranch string `json:"base_branch"`
	WorkBranch string `json:"work_branch"`
}

type SubmitResponse struct {
	SandboxTaskID string `json:"sandbox_task_id"`
	Status        string `json:"status,omitempty"`
}

type Result struct {
	Diff          []FileChange `json:"diff"`
	TestsPassed   bool         `json:"tests_passed"`
	Summary       string       `json:"summary"`
	CostUSDMicros int64        `json:"cost_usd_micros"`
	Error         string       `json:"error,omitempty"`
}

type FileChange struct {
	Path    string `json:"path"`
	Content string `json:"content"`
}

type LeaseView struct {
	TTLSeconds  int    `json:"ttl_seconds"`
	HeartbeatAt string `json:"heartbeat_at"`
	ExpiresAt   string `json:"expires_at"`
	Expired     bool   `json:"expired"`
}

type StatusResponse struct {
	Status   string     `json:"status"`
	Phase    string     `json:"phase,omitempty"`
	Reason   string     `json:"reason,omitempty"`
	Error    string     `json:"error,omitempty"`
	Lease    *LeaseView `json:"lease,omitempty"`
	Result   *Result    `json:"result,omitempty"`
	// Honesty flags — never claim Firecracker from the in-repo scaffold.
	Backend         string `json:"backend"`
	FirecrackerLive bool   `json:"firecracker_live"`
	CodingClaimed   bool   `json:"coding_claimed"`
}

type ErrorBody struct {
	Error  string `json:"error"`
	Reason string `json:"reason"`
	Code   string `json:"code"`
}

func IsTerminal(status string) bool {
	switch strings.ToLower(strings.TrimSpace(status)) {
	case StatusDone, StatusFailed, StatusTimeout:
		return true
	default:
		return false
	}
}

func IsEcho(req SubmitRequest) bool {
	return strings.EqualFold(strings.TrimSpace(req.Engine), "echo") && req.TestMode
}

// ValidateSubmit mirrors Python validate_submit_request. Empty = valid.
// Echo + test_mode is a diagnostic: repo is optional.
func ValidateSubmit(req SubmitRequest) []string {
	var problems []string
	if req.ContractVersion != Version {
		problems = append(problems, fmt.Sprintf("contract_version must be %s", Version))
	}
	if strings.TrimSpace(req.TaskID) == "" {
		problems = append(problems, "task_id is required")
	}
	if req.EngineEnv == nil {
		problems = append(problems, "engine_env must be an object")
	}
	if !IsEcho(req) {
		if strings.TrimSpace(req.Repo.Owner) == "" || strings.TrimSpace(req.Repo.Name) == "" {
			problems = append(problems, "repo.owner and repo.name are required")
		}
	}
	return problems
}

func DecodeSubmit(raw []byte) (SubmitRequest, error) {
	var req SubmitRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		return req, fmt.Errorf("%s: %w", ReasonContractInvalid, err)
	}
	if req.EngineEnv == nil {
		// JSON null / omitted object — treat as missing so ValidateSubmit reports it.
		// Echo bodies from Python send {}.
	}
	if req.TenantID == "" {
		req.TenantID = "default"
	}
	return req, nil
}

func EngineLooksLikeCoding(engine string) bool {
	e := strings.ToLower(strings.TrimSpace(engine))
	switch e {
	case "", "echo":
		return false
	default:
		return true
	}
}
