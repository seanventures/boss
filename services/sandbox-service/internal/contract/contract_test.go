package contract

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func testdata(t *testing.T, name string) []byte {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime.Caller failed")
	}
	path := filepath.Join(filepath.Dir(file), "..", "..", "testdata", name)
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return raw
}

func TestGoldenSubmitShape(t *testing.T) {
	raw := testdata(t, "submit_valid.json")
	req, err := DecodeSubmit(raw)
	if err != nil {
		t.Fatal(err)
	}
	if req.ContractVersion != Version {
		t.Fatalf("contract_version=%q", req.ContractVersion)
	}
	if req.TaskID != "crun_s1_contract" {
		t.Fatalf("task_id=%q", req.TaskID)
	}
	if req.Repo.Owner != "example-user" || req.Repo.Name != "boss-sandbox" {
		t.Fatalf("repo=%+v", req.Repo)
	}
	if req.Engine != "claude_code" {
		t.Fatalf("engine=%q", req.Engine)
	}
	if req.Handles["mdl"] != "boss_mdl_example-handle-not-a-credential" || req.Handles["egr"] == "" {
		t.Fatalf("handles=%v", req.Handles)
	}
	if req.Endpoints["model"] != "http://127.0.0.1:7101" {
		t.Fatalf("endpoints=%v", req.Endpoints)
	}
	if req.GitToken != "" || req.EngineEnv != nil {
		t.Fatalf("a v1.1 body carries no credential field: %q %v", req.GitToken, req.EngineEnv)
	}
	if problems := ValidateSubmit(req); len(problems) != 0 {
		t.Fatalf("expected valid golden submit, got %v", problems)
	}
}

func TestValidateSubmitRejectsUnknownVersionAndMissingFields(t *testing.T) {
	req := SubmitRequest{ContractVersion: "0.9", Handles: map[string]string{}, Endpoints: map[string]string{}}
	problems := ValidateSubmit(req)
	joined := ""
	for _, p := range problems {
		joined += p + ";"
	}
	if !contains(joined, "contract_version") {
		t.Fatalf("missing contract_version problem: %v", problems)
	}
	if !contains(joined, "task_id") {
		t.Fatalf("missing task_id problem: %v", problems)
	}
	if !contains(joined, "repo.owner") {
		t.Fatalf("missing repo.owner problem: %v", problems)
	}
}

func TestEchoSkipsRepoRequirement(t *testing.T) {
	req := SubmitRequest{
		ContractVersion: Version,
		TaskID:          "exercise_echo_api",
		Engine:          "echo",
		TestMode:        true,
		Handles:         map[string]string{},
		Endpoints:       map[string]string{},
	}
	if problems := ValidateSubmit(req); len(problems) != 0 {
		t.Fatalf("echo should not require repo: %v", problems)
	}
}

// P8b S9: the legacy fixture is refused by default and by every strict host, accepted
// (with its engine_env rules) only on the legacy lane; a v1.1 body never carries a credential.
func TestLegacyBodyOnlyOnTheLegacyLane(t *testing.T) {
	req, err := DecodeSubmit(testdata(t, "submit_legacy_v10.json"))
	if err != nil {
		t.Fatal(err)
	}
	if p := ValidateSubmit(req); len(p) == 0 || RefusalReason(p) != ReasonContractLegacyRefused {
		t.Fatalf("default options must refuse v1.0: %v", p)
	}
	if p := ValidateSubmitWith(req, Options{StrictSecrets: true, AllowLegacy: true}); len(p) == 0 ||
		RefusalReason(p) != ReasonContractLegacyRefused {
		t.Fatalf("a strict host must refuse v1.0 even on the legacy lane: %v", p)
	}
	if p := ValidateSubmitWith(req, Options{StrictSecrets: false, AllowLegacy: true}); len(p) != 0 {
		t.Fatalf("the legacy lane accepts the v1.0 golden: %v", p)
	}
	v11, _ := DecodeSubmit(testdata(t, "submit_valid.json"))
	v11.Raw["git_token"] = "ghs_value-not-printed"
	p := ValidateSubmit(v11)
	if RefusalReason(p) != ReasonRawCredentialField || contains(strings.Join(p, ";"), "value-not-printed") {
		t.Fatalf("raw credential field: %v", p)
	}
	v11, _ = DecodeSubmit(testdata(t, "submit_valid.json"))
	v11.Raw["instructions"] = "use AKIAIOSFODNN7EXAMPLE to log in"
	if RefusalReason(ValidateSubmit(v11)) != ReasonSecretShapedValue {
		t.Fatalf("a key shape anywhere in a strict body is refused: %v", ValidateSubmit(v11))
	}
	if p := ValidateSubmitWith(v11, Options{StrictSecrets: false}); len(p) != 0 {
		t.Fatalf("a non-strict host judges fields, not shapes: %v", p)
	}
	if OptionsFromEnv() != DefaultOptions {
		t.Fatalf("strict on, legacy off by default: %+v", OptionsFromEnv())
	}
}

func TestIsTerminal(t *testing.T) {
	if !IsTerminal("done") || !IsTerminal("failed") || !IsTerminal("timeout") {
		t.Fatal("terminal statuses")
	}
	if IsTerminal("running") || IsTerminal("queued") || IsTerminal("") {
		t.Fatal("non-terminal treated as terminal")
	}
}

func TestErrorBodyJSONShape(t *testing.T) {
	raw, err := json.Marshal(ErrorBody{
		Error:  "microVM execution is not claimed for Community GA",
		Reason: ReasonClaimsFrozen,
		Code:   ReasonClaimsFrozen,
	})
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]string
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	if got["reason"] != ReasonClaimsFrozen || got["code"] != ReasonClaimsFrozen {
		t.Fatalf("got %v", got)
	}
}

func contains(s, sub string) bool {
	return len(s) >= len(sub) && (s == sub || len(sub) == 0 ||
		(func() bool {
			for i := 0; i+len(sub) <= len(s); i++ {
				if s[i:i+len(sub)] == sub {
					return true
				}
			}
			return false
		})())
}
