package contract

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// The golden submit fixture (shared with the Python contract tests) must map
// back into the FLAT guest spec the in-guest agent reads — the reverse of
// Python's _ENGINE_ENV_MAP.
func TestGuestSpecFromGoldenSubmit(t *testing.T) {
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("caller")
	}
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(file), "..", "..", "testdata", "submit_valid.json"))
	if err != nil {
		t.Fatal(err)
	}
	req, err := DecodeSubmit(raw)
	if err != nil {
		t.Fatal(err)
	}
	spec := GuestSpec(req)
	if spec["owner"] != "example-user" || spec["repo"] != "boss-sandbox" {
		t.Fatalf("repo mapping: %v", spec)
	}
	if spec["engine"] != "claude_code" {
		t.Fatalf("engine: %v", spec["engine"])
	}
	if spec["test_mode"] != false {
		t.Fatalf("coding spec must not run in test_mode: %v", spec["test_mode"])
	}
	if spec["base_branch"] != "main" {
		t.Fatalf("base_branch: %v", spec["base_branch"])
	}
	// v1.1: handles, endpoints and mcp ride into the guest; no credential field does.
	handles, _ := spec["handles"].(map[string]string)
	if handles["mdl"] == "" || !strings.HasPrefix(handles["mdl"], "boss_mdl_") {
		t.Fatalf("handles.mdl must reach the guest: %v", spec)
	}
	if spec["endpoints"].(map[string]string)["model"] != "http://127.0.0.1:7101" {
		t.Fatalf("endpoints must reach the guest: %v", spec)
	}
	for _, field := range []string{"token", "claude_oauth_token", "openai_api_key", "azure_key"} {
		if _, ok := spec[field]; ok {
			t.Fatalf("a v1.1 guest spec carries %s", field)
		}
	}
	// The legacy fixture still flattens engine_env names to the guest's fields (frozen lane).
	legacy, err := DecodeSubmit(func() []byte {
		raw, err := os.ReadFile(filepath.Join(filepath.Dir(file), "..", "..", "testdata", "submit_legacy_v10.json"))
		if err != nil {
			t.Fatal(err)
		}
		return raw
	}())
	if err != nil {
		t.Fatal(err)
	}
	if GuestSpec(legacy)["claude_oauth_token"] != "tok" {
		t.Fatalf("CLAUDE_CODE_OAUTH_TOKEN must map to claude_oauth_token on the legacy lane: %v", GuestSpec(legacy))
	}
	// The spec must stay valid JSON (it is written to /work/spec.json).
	if _, err := json.Marshal(spec); err != nil {
		t.Fatal(err)
	}
}

func TestGuestSpecEchoIsAlwaysOfflineTestMode(t *testing.T) {
	spec := GuestSpec(SubmitRequest{
		ContractVersion: Version,
		TaskID:          "diag-roundtrip",
		Engine:          "echo",
		Repo:            RepoSpec{Owner: "o", Name: "r", BaseBranch: "main"},
		Handles:         map[string]string{},
		Endpoints:       map[string]string{},
	})
	if spec["test_mode"] != true {
		t.Fatalf("echo must force test_mode (no network, no credentials): %v", spec)
	}
}

func TestTimeoutSecondsCoercion(t *testing.T) {
	cases := map[any]int{
		float64(90): 90,
		"120":       120,
		nil:         0,
		"junk":      0,
	}
	for in, want := range cases {
		if got := TimeoutSeconds(in); got != want {
			t.Fatalf("TimeoutSeconds(%v)=%d want %d", in, got, want)
		}
	}
}

func TestCancelledIsTerminal(t *testing.T) {
	if !IsTerminal(StatusCancelled) {
		t.Fatal("cancelled is a terminal status (Python _TERMINAL includes it)")
	}
}

// P8pre-T5 / S1: a claude_code guest spec stages only its own engine's
// credential; an OpenAI or Azure key is never written into its work drive.
func TestGuestSpecStagesOnlyTheSelectedEnginesCredential(t *testing.T) {
	legacy := Options{StrictSecrets: false, AllowLegacy: true}
	req := SubmitRequest{ContractVersion: LegacyVersion, TaskID: "crun_x", Engine: "claude_code",
		Repo: RepoSpec{Owner: "o", Name: "r"},
		EngineEnv: map[string]string{"CLAUDE_CODE_OAUTH_TOKEN": "tok", "OPENAI_API_KEY": "k",
			"AZURE_OPENAI_KEY": "az"}}
	spec := GuestSpec(req)
	for _, field := range []string{"openai_api_key", "azure_key"} {
		if _, ok := spec[field]; ok {
			t.Fatalf("claude_code guest spec carries %s", field)
		}
	}
	if spec["claude_oauth_token"] != "tok" {
		t.Fatalf("claude token missing: %v", spec["claude_oauth_token"] != nil)
	}
	problems := ValidateSubmitWith(req, legacy)
	if len(problems) != 2 || !strings.HasPrefix(problems[0], ReasonEngineEnvNotForEngine+": engine_env AZURE_OPENAI_KEY") {
		t.Fatalf("problems=%v", problems)
	}
	open := SubmitRequest{ContractVersion: LegacyVersion, TaskID: "crun_y", Engine: "openai",
		Repo:      RepoSpec{Owner: "o", Name: "r"},
		EngineEnv: map[string]string{"OPENAI_API_KEY": "k", "SOMETHING_ELSE": "v"}}
	problems = ValidateSubmitWith(open, legacy)
	if len(problems) != 1 || !strings.HasPrefix(problems[0], ReasonEngineEnvUnknown+": engine_env SOMETHING_ELSE") {
		t.Fatalf("problems=%v", problems)
	}
	if GuestSpec(open)["openai_api_key"] != "k" {
		t.Fatal("openai guest must keep its own key")
	}
}

// BOSSAPP-1286: the legacy lane carries the token's host to the guest, and a git token
// without github_web is refused instead of defaulting to github.com.
func TestLegacyTokenCarriesItsHostOrIsRefused(t *testing.T) {
	opts := DefaultOptions
	opts.AllowLegacy = true
	opts.StrictSecrets = false
	base := SubmitRequest{ContractVersion: LegacyVersion, TaskID: "crun_ghes", Engine: "openai",
		Repo: RepoSpec{Owner: "o", Name: "r"}, GitToken: "ghes-token-not-printed",
		EngineEnv: map[string]string{}}
	found := false
	for _, p := range ValidateSubmitWith(base, opts) {
		if p == "repo.github_web is required with git_token" {
			found = true
		}
	}
	if !found {
		t.Fatalf("a legacy git token without github_web must be refused")
	}
	withHost := base
	withHost.Repo.GithubWeb = "https://ghe.example"
	withHost.Repo.Host = "github"
	for _, p := range ValidateSubmitWith(withHost, opts) {
		if p == "repo.github_web is required with git_token" {
			t.Fatalf("a token with its host must pass: %v", p)
		}
	}
	spec := GuestSpec(withHost)
	if spec["github_web"] != "https://ghe.example" || spec["repo_host"] != "github" {
		t.Fatalf("the guest spec must carry the token's host: %v %v", spec["github_web"], spec["repo_host"])
	}
}
