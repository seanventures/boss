// Package contract is the Go side of the versioned Python ⇄ sandbox-service
// job API (docs/infra/PYTHON_GO_SANDBOX_CONTRACT.md). Python owns the
// envelope; this package conforms. Changes are additive.
//
// v1.1 (P8b S9, BOSSAPP-1070): NO credential in the body. The v1.0 fields
// git_token and engine_env are gone; the guest gets opaque handles
// (boss_egr_/boss_mdl_/boss_mcpgw_/boss_run_), the guest-loopback endpoints
// boss-vsockd bridges, and the MCP gateway URLs. A strict_secrets host refuses
// any body carrying a raw credential field or a secret-shaped value. v1.0 is
// accepted only on the frozen legacy lane (Options.AllowLegacy), with a
// deprecation log line, and never by a strict host.
package contract

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"regexp"
	"sort"
	"strings"
)

const Version = "1.1"

// LegacyVersion is the frozen v1.0 lane, which carried credentials into the guest.
const LegacyVersion = "1.0"

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
	// guest field (P8-pre S5; v1.0 bodies only). A name the guest cannot read used
	// to be dropped silently (the Anthropic API key, defect 1); now the submit is refused.
	ReasonEngineEnvUnknown = "engine_env_unknown"
	// ReasonEngineEnvNotForEngine names a known credential sent to a guest whose
	// engine never reads it (an OpenAI or Azure key on a claude_code guest).
	ReasonEngineEnvNotForEngine = "engine_env_not_for_engine"
	// ReasonSecretPath: the fs endpoint never serves spec.json, .git/config or
	// *credentials* paths of a retained workspace (P8-pre S2).
	ReasonSecretPath = "secret_path_refused"
	// ReasonRawCredentialField: a v1.1 body carries a field a credential rode under
	// (git_token, engine_env, token, …) or a handle that is not a boss_ handle (P8b S9).
	ReasonRawCredentialField = "raw_credential_field"
	// ReasonSecretShapedValue: on a strict_secrets host, a secret-named field holds a
	// value, or a well-known key shape appears anywhere in the body (P8b S9).
	ReasonSecretShapedValue = "secret_shaped_value"
	// ReasonContractDeprecated is the log line a v1.0 body earns on the legacy lane.
	ReasonContractDeprecated = "contract_deprecated"
	// ReasonContractLegacyRefused: a v1.0 body outside the legacy lane, or on a strict host.
	ReasonContractLegacyRefused = "contract_legacy_refused"
)

// SubmitRequest is the typed POST /v1/sandbox/tasks body (v1.1). Legacy v1.0
// fields are decoded for the frozen lane only and never honoured by a strict host.
type SubmitRequest struct {
	ContractVersion    string         `json:"contract_version"`
	TaskID             string         `json:"task_id"`
	TenantID           string         `json:"tenant_id"`
	Repo               RepoSpec       `json:"repo"`
	Instructions       string         `json:"instructions"`
	AcceptanceCriteria []string       `json:"acceptance_criteria"`
	TestCommands       []string       `json:"test_commands"`
	Engine             string         `json:"engine"`
	RunMutation        bool           `json:"run_mutation"`
	MaxTurns           *int           `json:"max_turns"`
	Timeout            any            `json:"timeout"`
	Resources          map[string]any `json:"resources"`
	TestMode           bool           `json:"test_mode"`
	// v1.1: the guest's placeholders, the loopback ports, the MCP gateway URLs.
	Handles   map[string]string `json:"handles"`
	Endpoints map[string]string `json:"endpoints"`
	MCP       *MCPSpec          `json:"mcp,omitempty"`
	// v1.0 (legacy lane only).
	GitToken  string            `json:"git_token,omitempty"`
	EngineEnv map[string]string `json:"engine_env,omitempty"`
	// Raw is the decoded body as sent, for the credential scan (never logged).
	Raw map[string]any `json:"-"`
}

// MCPSpec carries the MCP gateway URLs the guest reaches on its mcp endpoint; the
// credential is handles.mcpgw, never here.
type MCPSpec struct {
	Servers map[string]string `json:"servers"`
}

type RepoSpec struct {
	Owner      string `json:"owner"`
	Name       string `json:"name"`
	BaseBranch string `json:"base_branch"`
	WorkBranch string `json:"work_branch"`
	// Host is the git host kind ("github", "bitbucket"); GithubWeb the web origin of the
	// GitHub the git token belongs to (github.com or a GHES origin). Additive (BOSSAPP-1286):
	// both are forwarded to the guest so a token is cloned only from its own host.
	Host      string `json:"host,omitempty"`
	GithubWeb string `json:"github_web,omitempty"`
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

// Options: how a validator judges a body. Python's validate_submit_request
// takes the same two switches (strict_secrets, allow_legacy).
type Options struct {
	// StrictSecrets: the host refuses any secret-shaped value (and every v1.0 body).
	// Every microVM host is strict by default (hostd facts: strict_secrets).
	StrictSecrets bool
	// AllowLegacy: the frozen v1.0 lane (BOSS_SANDBOX_CONTRACT_LEGACY=1 on both sides).
	AllowLegacy bool
}

// DefaultOptions is what a strict host enforces: v1.1 only, no secret shape.
var DefaultOptions = Options{StrictSecrets: true, AllowLegacy: false}

// OptionsFromEnv reads the service's switches: BOSS_STRICT_SECRETS (default on;
// "0"/"false" turns it off) and BOSS_SANDBOX_CONTRACT_LEGACY (default off).
func OptionsFromEnv() Options {
	off := func(v string) bool {
		switch strings.ToLower(strings.TrimSpace(v)) {
		case "0", "false", "no", "off":
			return true
		}
		return false
	}
	on := func(v string) bool {
		switch strings.ToLower(strings.TrimSpace(v)) {
		case "1", "true", "yes", "on":
			return true
		}
		return false
	}
	return Options{StrictSecrets: !off(os.Getenv("BOSS_STRICT_SECRETS")),
		AllowLegacy: on(os.Getenv("BOSS_SANDBOX_CONTRACT_LEGACY"))}
}

// HandlePrefixes: each guest handle kind and the prefix it must carry.
var HandlePrefixes = map[string]string{
	"egr": "boss_egr_", "mdl": "boss_mdl_", "mcpgw": "boss_mcpgw_", "run": "boss_run_",
}

// RawCredentialFields never appear in a v1.1 body (Python RAW_CREDENTIAL_FIELDS).
var RawCredentialFields = []string{
	"git_token", "engine_env", "token", "claude_oauth_token", "anthropic_api_key",
	"openai_api_key", "azure_key", "github_token", "gh_token", "api_key", "password",
}

var (
	endpointRe = regexp.MustCompile(`^http://127\.0\.0\.1:71\d\d$`)
	// secretName / notSecretName mirror Python sandbox_contract._SECRET_NAME (env_refs' shape
	// minus the bare _KEY$, which contract bodies use for ids) and env_refs._NOT_SECRET_NAME.
	secretName    = regexp.MustCompile(`(TOKEN|SECRET|PASSWORD|PASSPHRASE|_PAT$|API_KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL)`)
	notSecretName = regexp.MustCompile(`(_ID|_FILE|_PATH|_URL|_REGION|_HOST|_USER|_USERNAME|_NAME|_EMAIL)$`)
	secretValue   = regexp.MustCompile(`(sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{32,}|gh[pousr]_[A-Za-z0-9]{30,}` +
		`|github_pat_[A-Za-z0-9_]{40,}|glpat-[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{20,}` +
		`|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})`)
)

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

// IsLegacy reports a v1.0 body.
func IsLegacy(req SubmitRequest) bool { return req.ContractVersion == LegacyVersion }

// ValidateSubmit mirrors Python validate_submit_request with DefaultOptions
// (strict, no legacy). Empty = valid. Echo + test_mode is a diagnostic: repo is optional.
func ValidateSubmit(req SubmitRequest) []string { return ValidateSubmitWith(req, DefaultOptions) }

// ValidateSubmitWith judges a body under the host's options. Problems carry field
// NAMES only, never a value.
func ValidateSubmitWith(req SubmitRequest, opts Options) []string {
	var problems []string
	switch req.ContractVersion {
	case LegacyVersion:
		switch {
		case !opts.AllowLegacy:
			problems = append(problems, fmt.Sprintf("%s: contract_version %s is the frozen legacy lane; send %s",
				ReasonContractLegacyRefused, LegacyVersion, Version))
		case opts.StrictSecrets:
			problems = append(problems, fmt.Sprintf("%s: contract_version %s carries credentials and is refused by a strict_secrets host",
				ReasonContractLegacyRefused, LegacyVersion))
		default:
			log.Printf("%s: a contract v%s submit was accepted on the legacy lane (task %s)",
				ReasonContractDeprecated, LegacyVersion, req.TaskID)
			if req.EngineEnv == nil {
				problems = append(problems, "engine_env must be an object")
			}
			// A git token without the web origin it belongs to would be cloned from a
			// default host (BOSSAPP-1286): refused, never defaulted.
			if req.GitToken != "" && (req.Repo.Host == "" || req.Repo.Host == "github") &&
				strings.TrimSpace(req.Repo.GithubWeb) == "" {
				problems = append(problems, "repo.github_web is required with git_token")
			}
			problems = append(problems, EngineEnvProblems(req.Engine, req.EngineEnv)...)
		}
	case Version:
		problems = append(problems, SecretProblems(req, opts.StrictSecrets)...)
		if req.Handles == nil {
			problems = append(problems, "handles must be an object")
		}
		if req.Endpoints == nil {
			problems = append(problems, "endpoints must be an object")
		} else {
			names := make([]string, 0, len(req.Endpoints))
			for n := range req.Endpoints {
				names = append(names, n)
			}
			sort.Strings(names)
			for _, n := range names {
				if !endpointRe.MatchString(req.Endpoints[n]) {
					problems = append(problems, fmt.Sprintf("endpoints.%s must be a guest-loopback 71xx URL", n))
				}
			}
		}
	default:
		problems = append(problems, fmt.Sprintf("contract_version must be %s", Version))
	}
	if strings.TrimSpace(req.TaskID) == "" {
		problems = append(problems, "task_id is required")
	}
	if !IsEcho(req) {
		if strings.TrimSpace(req.Repo.Owner) == "" || strings.TrimSpace(req.Repo.Name) == "" {
			problems = append(problems, "repo.owner and repo.name are required")
		}
	}
	return problems
}

// SecretProblems: where a v1.1 body carries a credential (Python secret_problems,
// same order and wording). Raw credential fields and non-handle "handles" always;
// secret-shaped values only when strict.
func SecretProblems(req SubmitRequest, strict bool) []string {
	var problems []string
	raw := req.Raw
	if raw == nil {
		raw = map[string]any{}
		if req.GitToken != "" {
			raw["git_token"] = req.GitToken
		}
		if req.EngineEnv != nil {
			raw["engine_env"] = req.EngineEnv
		}
	}
	present := make([]string, 0, 2)
	for _, name := range RawCredentialFields {
		if _, ok := raw[name]; ok {
			present = append(present, name)
		}
	}
	sort.Strings(present)
	for _, name := range present {
		problems = append(problems, fmt.Sprintf("%s: %s is not a v1.1 field; the guest holds handles", ReasonRawCredentialField, name))
	}
	kinds := make([]string, 0, len(HandlePrefixes))
	for k := range HandlePrefixes {
		kinds = append(kinds, k)
	}
	sort.Strings(kinds)
	for _, kind := range kinds {
		if v, ok := req.Handles[kind]; ok && v != "" && !strings.HasPrefix(v, HandlePrefixes[kind]) {
			problems = append(problems, fmt.Sprintf("%s: handles.%s is not a %s handle", ReasonRawCredentialField, kind, HandlePrefixes[kind]))
		}
	}
	if strict && req.Raw != nil {
		problems = append(problems, walk(req.Raw, "")...)
	}
	return problems
}

func nameCarriesSecret(name string) bool {
	upper := strings.ToUpper(name)
	return secretName.MatchString(upper) && !notSecretName.MatchString(upper)
}

func walk(node any, path string) []string {
	var out []string
	switch v := node.(type) {
	case map[string]any:
		keys := make([]string, 0, len(v))
		for k := range v {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			here := k
			if path != "" {
				here = path + "." + k
			}
			s, isStr := v[k].(string)
			if strings.HasPrefix(here, "handles.") && isStr && strings.HasPrefix(s, "boss_") {
				continue
			}
			if isStr && s != "" && nameCarriesSecret(k) && !strings.HasPrefix(s, "boss_") {
				out = append(out, fmt.Sprintf("%s: %s is named like a secret and holds a value", ReasonSecretShapedValue, here))
				continue
			}
			out = append(out, walk(v[k], here)...)
		}
	case []any:
		for i, item := range v {
			out = append(out, walk(item, fmt.Sprintf("%s[%d]", path, i))...)
		}
	case string:
		if secretValue.MatchString(v) {
			where := path
			if where == "" {
				where = "body"
			}
			out = append(out, fmt.Sprintf("%s: %s holds a credential-shaped value", ReasonSecretShapedValue, where))
		}
	}
	return out
}

// RefusalReason names the first problem's reason code (for the 422 body), or
// ReasonContractInvalid.
func RefusalReason(problems []string) string {
	if len(problems) == 0 {
		return ""
	}
	for _, named := range []string{ReasonRawCredentialField, ReasonSecretShapedValue,
		ReasonContractLegacyRefused, ReasonEngineEnvUnknown, ReasonEngineEnvNotForEngine} {
		if strings.HasPrefix(problems[0], named+":") {
			return named
		}
	}
	return ReasonContractInvalid
}

func DecodeSubmit(raw []byte) (SubmitRequest, error) {
	var req SubmitRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		return req, fmt.Errorf("%s: %w", ReasonContractInvalid, err)
	}
	var generic map[string]any
	if err := json.Unmarshal(raw, &generic); err == nil {
		req.Raw = generic
	}
	if req.TenantID == "" {
		req.TenantID = "default"
	}
	return req, nil
}

// engineEnvToSpec is the reverse of Python sandbox_contract._ENGINE_ENV_MAP
// (v1.0, legacy lane only): env-var name (as sent in engine_env) -> flat
// guest-spec field the in-guest agent reads.
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
// guest cannot read and every credential not meant for this engine (v1.0).
// Names only: a value never appears in a problem.
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
//
// v1.1: the spec carries `handles`, `endpoints` and `mcp` and NO credential;
// the guest points its model client at endpoints.model with handles.mdl and
// its git at endpoints.git with handles.egr. v1.0 (legacy lane) still stages
// the token and the engine's credential, as before.
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
		"contract_version":    req.ContractVersion,
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
	if req.Repo.Host != "" {
		spec["repo_host"] = req.Repo.Host
	}
	if req.Repo.GithubWeb != "" {
		spec["github_web"] = req.Repo.GithubWeb
	}
	if IsLegacy(req) {
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
	} else {
		handles := map[string]string{}
		for k, v := range req.Handles {
			if v != "" {
				handles[k] = v
			}
		}
		spec["handles"] = handles
		endpoints := map[string]string{}
		for k, v := range req.Endpoints {
			endpoints[k] = v
		}
		spec["endpoints"] = endpoints
		if req.MCP != nil {
			spec["mcp"] = map[string]any{"servers": req.MCP.Servers}
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
