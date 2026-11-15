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
	// engine_env env-var names flatten to the guest's field names.
	if spec["claude_oauth_token"] == nil || spec["claude_oauth_token"] == "" {
		t.Fatalf("CLAUDE_CODE_OAUTH_TOKEN must map to claude_oauth_token: %v", spec)
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
		GitToken:        "tok",
		EngineEnv:       map[string]string{},
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
	req := SubmitRequest{ContractVersion: Version, TaskID: "crun_x", Engine: "claude_code",
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
	problems := ValidateSubmit(req)
	if len(problems) != 2 || !strings.HasPrefix(problems[0], ReasonEngineEnvNotForEngine+": engine_env AZURE_OPENAI_KEY") {
		t.Fatalf("problems=%v", problems)
	}
	open := SubmitRequest{ContractVersion: Version, TaskID: "crun_y", Engine: "openai",
		Repo:      RepoSpec{Owner: "o", Name: "r"},
		EngineEnv: map[string]string{"OPENAI_API_KEY": "k", "SOMETHING_ELSE": "v"}}
	problems = ValidateSubmit(open)
	if len(problems) != 1 || !strings.HasPrefix(problems[0], ReasonEngineEnvUnknown+": engine_env SOMETHING_ELSE") {
		t.Fatalf("problems=%v", problems)
	}
	if GuestSpec(open)["openai_api_key"] != "k" {
		t.Fatal("openai guest must keep its own key")
	}
}
