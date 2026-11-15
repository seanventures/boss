package httpapi

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/backend"
	"github.com/seanheiney/boss/services/sandbox-service/internal/contract"
	"github.com/seanheiney/boss/services/sandbox-service/internal/job"
	"github.com/seanheiney/boss/services/sandbox-service/internal/run"
)

const leakedToken = "ghs_TESTONLYTOKENxxxxxxxxxxxxxxxxxxxxxxxx"

// leakyBackend leaves behind what a careless guest run would: world-readable
// files, a spec copy, a token in .git/config and a credentials file, then
// reports a failure — the case that is retained and browsable for two hours.
type leakyBackend struct{}

func (leakyBackend) Name() string { return "leaky" }
func (leakyBackend) Check() error { return nil }
func (leakyBackend) Run(_ context.Context, r backend.RunRequest) error {
	repo := filepath.Join(r.TaskDir, "work", "repo")
	gitDir := filepath.Join(repo, ".git")
	if err := os.MkdirAll(gitDir, 0o755); err != nil {
		return err
	}
	_ = os.MkdirAll(filepath.Join(r.TaskDir, "workstage"), 0o755)
	_ = os.WriteFile(filepath.Join(r.TaskDir, "workstage", "spec.json"), []byte(leakedToken), 0o644)
	_ = os.WriteFile(filepath.Join(r.TaskDir, "vm.json"), []byte("{}"), 0o644)
	_ = os.WriteFile(filepath.Join(gitDir, "config"),
		[]byte("[remote \"origin\"]\n\turl = https://x-access-token:"+leakedToken+"@github.com/o/r.git\n"), 0o644)
	_ = os.WriteFile(filepath.Join(repo, ".credentials.json"), []byte(leakedToken), 0o644)
	_ = os.WriteFile(filepath.Join(repo, "README.md"), []byte("hi"), 0o644)
	_ = os.WriteFile(filepath.Join(repo, "spec.json"), []byte(leakedToken), 0o644)
	return os.WriteFile(r.ResultPath, []byte(`{"status":"error","error":"tests failed"}`), 0o644)
}

// P8pre-T1 + P8pre-T2: after a failed task, every file under the task directory
// is owner-only, no spec survives, .git/config is credential-free, and the fs
// endpoint refuses spec.json, .git/config and *credentials* paths.
func TestFailedTaskIsRetainedScrubbedAndOwnerOnly(t *testing.T) {
	store := job.NewStore(false)
	b := leakyBackend{}
	store.ConfigureBackend(b.Name(), b.Check)
	root := t.TempDir()
	srv := NewWithRunner(store, "tok", run.New(store, b, root))

	body, _ := json.Marshal(map[string]any{
		"contract_version": "1.0", "task_id": "crun_leaky", "tenant_id": "default",
		"engine": "claude_code", "engine_env": map[string]string{"CLAUDE_CODE_OAUTH_TOKEN": leakedToken},
		"repo": map[string]string{"owner": "o", "name": "r"}, "git_token": leakedToken,
	})
	res := doJSON(t, srv, http.MethodPost, "/v1/sandbox/tasks", body, "tok")
	if res.StatusCode != http.StatusAccepted {
		t.Fatalf("submit status=%d", res.StatusCode)
	}
	var submitted contract.SubmitResponse
	if err := json.NewDecoder(res.Body).Decode(&submitted); err != nil {
		t.Fatal(err)
	}
	status := pollUntilTerminal(t, srv, submitted.SandboxTaskID, 10*time.Second)
	if status.Status != contract.StatusFailed {
		t.Fatalf("status=%s", status.Status)
	}
	jb, _ := store.Get(submitted.SandboxTaskID)

	// T1: nothing under the task directory is wider than owner-only.
	_ = filepath.Walk(jb.TaskDir, func(p string, info os.FileInfo, err error) error {
		if err != nil {
			t.Fatal(err)
		}
		limit := os.FileMode(0o600)
		if info.IsDir() {
			limit = 0o700
		}
		if info.Mode().Perm()&^limit != 0 {
			t.Fatalf("%s is %o, wider than %o", p, info.Mode().Perm(), limit)
		}
		// T2: no file in the retained tree carries the token, except the
		// credentials file the fs endpoint refuses (it is the guest's own).
		if !info.IsDir() && !strings.Contains(info.Name(), "credentials") {
			raw, _ := os.ReadFile(p)
			if strings.Contains(string(raw), leakedToken) {
				t.Fatalf("%s still carries the token", p)
			}
		}
		if info.Name() == "spec.json" {
			t.Fatalf("a spec survived retention: %s", p)
		}
		return nil
	})

	// T2: the fs endpoint refuses the three shapes and hides them from listings.
	for _, p := range []string{"/work/repo/.git/config", "/work/repo/.credentials.json", "/work/spec.json"} {
		r := doJSON(t, srv, http.MethodGet, "/v1/sandbox/tasks/"+submitted.SandboxTaskID+"/fs?path="+p, nil, "tok")
		if r.StatusCode != http.StatusForbidden {
			t.Fatalf("fs %s status=%d, want 403", p, r.StatusCode)
		}
		var e contract.ErrorBody
		_ = json.NewDecoder(r.Body).Decode(&e)
		if e.Reason != contract.ReasonSecretPath {
			t.Fatalf("fs %s reason=%q", p, e.Reason)
		}
	}
	r := doJSON(t, srv, http.MethodGet, "/v1/sandbox/tasks/"+submitted.SandboxTaskID+"/fs?path=/work/repo", nil, "tok")
	var tree struct {
		Entries []struct {
			Name string `json:"name"`
		} `json:"entries"`
	}
	_ = json.NewDecoder(r.Body).Decode(&tree)
	names := []string{}
	for _, e := range tree.Entries {
		names = append(names, e.Name)
	}
	joined := strings.Join(names, ",")
	if strings.Contains(joined, "credentials") || !strings.Contains(joined, "README.md") {
		t.Fatalf("listing=%s", joined)
	}
}

// P8pre-T5 (Go half): an unknown engine_env name, and a credential sent to an
// engine that never reads it, are refused with named reasons.
func TestSubmitRefusesUnknownAndForeignEngineEnv(t *testing.T) {
	srv := New(job.NewStore(false), "tok")
	for env, reason := range map[string]string{
		"ANTHROPIC_API_KEY": contract.ReasonEngineEnvUnknown,
		"OPENAI_API_KEY":    contract.ReasonEngineEnvNotForEngine,
	} {
		body, _ := json.Marshal(map[string]any{
			"contract_version": "1.0", "task_id": "crun_env", "engine": "claude_code",
			"engine_env": map[string]string{env: "value-not-printed"},
			"repo":       map[string]string{"owner": "o", "name": "r"},
		})
		res := doJSON(t, srv, http.MethodPost, "/v1/sandbox/tasks", body, "tok")
		if res.StatusCode != http.StatusUnprocessableEntity {
			t.Fatalf("%s: status=%d", env, res.StatusCode)
		}
		var e contract.ErrorBody
		_ = json.NewDecoder(res.Body).Decode(&e)
		if e.Reason != reason || !strings.Contains(e.Error, env) || strings.Contains(e.Error, "value-not-printed") {
			t.Fatalf("%s: reason=%q error=%q", env, e.Reason, e.Error)
		}
	}
}
