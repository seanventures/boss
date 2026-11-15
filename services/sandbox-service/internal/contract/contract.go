// Package contract is the Go side of the versioned Python ⇄ sandbox-service
// job API (docs/infra/PYTHON_GO_SANDBOX_CONTRACT.md). Python owns the
// envelope; this package conforms. Changes are additive.
package contract

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
)

const Version = "1.0"

const (
	StatusQueued    = "queued"
	StatusRunning   = "running"
	StatusDone      = "done"
	StatusFailed    = "failed"
	StatusTimeout   = "timeout"
	StatusCancelled = "cancelled"
)

// Named fail-closed reasons. Never invent success; never drop these.
const (
	ReasonClaimsFrozen        = "microvm_claims_frozen"
	ReasonFirecrackerDisabled = "firecracker_not_enabled"
	ReasonContractInvalid     = "contract_invalid"
	ReasonUnauthorized        = "unauthorized"
	ReasonTaskMissing         = "sandbox_task_missing"
	ReasonLeaseExpired        = "lease_expired"
	ReasonBackendUnavailable  = "sandbox_backend_unavailable"
	ReasonPreviewUnsupported  = "preview_not_supported"
	ReasonCancelled           = "cancelled_by_request"
	// ReasonEngineEnvUnknown names an engine_env entry neither side maps to a
	// guest field (P8-pre S5). A name the guest cannot read used to be dropped
	// silently (the Anthropic API key, defect 1); now the submit is refused.
	ReasonEngineEnvUnknown = "engine_env_unknown"
	// ReasonEngineEnvNotForEngine names a known credential sent to a guest whose
	// engine never reads it (an OpenAI or Azure key on a claude_code guest).
	ReasonEngineEnvNotForEngine = "engine_env_not_for_engine"
	// ReasonSecretPath: the fs endpoint never serves spec.json, .git/config or
	// *credentials* paths of a retained workspace (P8-pre S2).
	ReasonSecretPath = "secret_path_refused"
)

// SubmitRequest is the typed POST /v1/sandbox/tasks body.
type SubmitRequest struct {
	ContractVersion    string            `json:"contract_version"`
	TaskID             string            `json:"task_id"`
	TenantID           string            `json:"tenant_id"`
	Repo               RepoSpec          `json:"repo"`
	GitToken           string            `json:"git_token"`
	Instructions       string            `json:"instructions"`
	AcceptanceCriteria []string          `json:"acceptance_criteria"`
	TestCommands       []string          `json:"test_commands"`
	Engine             string            `json:"engine"`
	EngineEnv          map[string]string `json:"engine_env"`
	RunMutation        bool              `json:"run_mutation"`
	MaxTurns           *int              `json:"max_turns"`
	Timeout            any               `json:"timeout"`
	Resources          map[string]any    `json:"resources"`
	TestMode           bool              `json:"test_mode"`
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
	// Additive fields the Python MicroVMProvider.run_code_task maps into a
	// CodeAgentResult (the in-guest agent's /work/result.json shape).
	Status      string         `json:"status,omitempty"` // "ok" | "error" from the guest
	Engine      string         `json:"engine,omitempty"`
	DiffSummary *DiffSummary   `json:"diff_summary,omitempty"`
	Tests       []TestExec     `json:"tests,omitempty"`
	Usage       map[string]any `json:"usage,omitempty"`
	Turns       int            `json:"turns,omitempty"`
}

// DiffSummary mirrors the guest's `diff_summary` (git numstat totals).
type DiffSummary struct {
	FilesChanged int      `json:"files_changed"`
	Additions    int      `json:"additions"`
	Deletions    int      `json:"deletions"`
	Paths        []string `json:"paths"`
}

// TestExec mirrors one entry of the guest's `tests` list; the captured output
// is the repair-loop evidence the control plane feeds back on retry.
type TestExec struct {
	Command    string `json:"command"`
	ExitCode   *int   `json:"exit_code"`
	DurationMS int    `json:"duration_ms"`
	TimedOut   bool   `json:"timed_out,omitempty"`
	Output     string `json:"output,omitempty"`
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
	Status string     `json:"status"`
	Phase  string     `json:"phase,omitempty"`
	Reason string     `json:"reason,omitempty"`
	Error  string     `json:"error,omitempty"`
	Lease  *LeaseView `json:"lease,omitempty"`
	Result *Result    `json:"result,omitempty"`
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
	case StatusDone, StatusFailed, StatusTimeout, StatusCancelled:
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
	problems = append(problems, EngineEnvProblems(req.Engine, req.EngineEnv)...)
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

// engineEnvToSpec is the reverse of Python sandbox_contract._ENGINE_ENV_MAP:
// env-var name (as sent in engine_env) -> flat guest-spec field the in-guest
// agent reads. Credentials ride per task; they are never baked into images.
var engineEnvToSpec = map[string]string{
	"CLAUDE_CODE_OAUTH_TOKEN":  "claude_oauth_token",
	"OPENAI_API_KEY":           "openai_api_key",
	"OPENAI_BASE_URL":          "openai_base_url",
	"OPENAI_MODEL":             "model",
	"AZURE_OPENAI_ENDPOINT":    "azure_endpoint",
	"AZURE_OPENAI_DEPLOYMENT":  "azure_deployment",
	"AZURE_OPENAI_API_VERSION": "azure_api_version",
	"AZURE_OPENAI_KEY":         "azure_key",
}

// engineEnvByEngine lists, per guest engine, the engine_env names that engine
// reads. Python's sandbox_contract.ENGINE_ENV_BY_ENGINE is the same table: a
// guest is sent only its own engine's credential (P8-pre S5).
var engineEnvByEngine = map[string][]string{
	"echo":        {},
	"claude_code": {"CLAUDE_CODE_OAUTH_TOKEN"},
	"openai": {"OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "AZURE_OPENAI_ENDPOINT",
		"AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_API_VERSION", "AZURE_OPENAI_KEY"},
	"native": {"OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "AZURE_OPENAI_ENDPOINT",
		"AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_API_VERSION", "AZURE_OPENAI_KEY"},
}

func engineReads(engine, name string) bool {
	names, known := engineEnvByEngine[strings.ToLower(strings.TrimSpace(engine))]
	if !known {
		return true // an engine this table does not list is not narrowed here
	}
	for _, n := range names {
		if n == name {
			return true
		}
	}
	return false
}

// EngineEnvProblems refuses, with a named reason, every engine_env name the
// guest cannot read and every credential not meant for this engine. Names
// only: a value never appears in a problem.
func EngineEnvProblems(engine string, env map[string]string) []string {
	names := make([]string, 0, len(env))
	for name := range env {
		names = append(names, name)
	}
	sort.Strings(names)
	var problems []string
	for _, name := range names {
		if _, ok := engineEnvToSpec[name]; !ok {
			problems = append(problems, fmt.Sprintf(
				"%s: engine_env %s is not a name the guest reads", ReasonEngineEnvUnknown, name))
			continue
		}
		if !engineReads(engine, name) {
			problems = append(problems, fmt.Sprintf(
				"%s: engine_env %s is not read by engine %s", ReasonEngineEnvNotForEngine, name,
				strings.ToLower(strings.TrimSpace(engine))))
		}
	}
	return problems
}

// TimeoutSeconds coerces the submit body's `timeout` (number, numeric string
// or absent) into seconds; 0 means "not set".
func TimeoutSeconds(v any) int {
	switch t := v.(type) {
	case float64:
		return int(t)
	case int:
		return t
	case string:
		var n int
		if _, err := fmt.Sscanf(strings.TrimSpace(t), "%d", &n); err == nil {
			return n
		}
	}
	return 0
}

// GuestSpec maps a contract SubmitRequest back into the FLAT spec the in-guest
// agent (sandbox-host/guest/in_guest_agent.py) reads from /work/spec.json.
// engine=echo always runs in test_mode: it is a deterministic diagnostic and
// must never need network, a repo, or credentials.
func GuestSpec(req SubmitRequest) map[string]any {
	engine := strings.ToLower(strings.TrimSpace(req.Engine))
	if engine == "" {
		engine = "echo"
	}
	spec := map[string]any{
		"task_id":             req.TaskID,
		"engine":              engine,
		"instructions":        req.Instructions,
		"acceptance_criteria": req.AcceptanceCriteria,
		"test_commands":       req.TestCommands,
		"run_mutation":        req.RunMutation,
		"test_mode":           req.TestMode || engine == "echo",
	}
	if req.Repo.Owner != "" {
		spec["owner"] = req.Repo.Owner
	}
	if req.Repo.Name != "" {
		spec["repo"] = req.Repo.Name
	}
	if req.Repo.BaseBranch != "" {
		spec["base_branch"] = req.Repo.BaseBranch
	}
	if req.Repo.WorkBranch != "" {
		spec["work_branch"] = req.Repo.WorkBranch
	}
	if req.GitToken != "" {
		spec["token"] = req.GitToken
	}
	for envName, field := range engineEnvToSpec {
		if !engineReads(engine, envName) {
			continue // never stage another engine's credential (P8-pre S1/S5)
		}
		if v, ok := req.EngineEnv[envName]; ok && v != "" {
			spec[field] = v
		}
	}
	if req.MaxTurns != nil && *req.MaxTurns > 0 {
		spec["max_turns"] = *req.MaxTurns
	} else if req.Resources != nil {
		if mt := TimeoutSeconds(req.Resources["max_turns"]); mt > 0 {
			spec["max_turns"] = mt
		}
	}
	if t := TimeoutSeconds(req.Timeout); t > 0 {
		spec["timeout"] = t
	}
	return spec
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
