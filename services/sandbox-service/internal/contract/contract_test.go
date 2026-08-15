package contract

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
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
	if req.Repo.Owner != "paramg" || req.Repo.Name != "boss-sandbox" {
		t.Fatalf("repo=%+v", req.Repo)
	}
	if req.Engine != "claude_code" {
		t.Fatalf("engine=%q", req.Engine)
	}
	if req.EngineEnv["CLAUDE_CODE_OAUTH_TOKEN"] != "tok" {
		t.Fatalf("engine_env=%v", req.EngineEnv)
	}
	if problems := ValidateSubmit(req); len(problems) != 0 {
		t.Fatalf("expected valid golden submit, got %v", problems)
	}
}

func TestValidateSubmitRejectsUnknownVersionAndMissingFields(t *testing.T) {
	req := SubmitRequest{ContractVersion: "0.9", EngineEnv: map[string]string{}}
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
		EngineEnv:       map[string]string{},
	}
	if problems := ValidateSubmit(req); len(problems) != 0 {
		t.Fatalf("echo should not require repo: %v", problems)
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
