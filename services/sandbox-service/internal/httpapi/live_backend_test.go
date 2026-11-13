package httpapi

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"testing"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/backend"
	"github.com/seanheiney/boss/services/sandbox-service/internal/contract"
	"github.com/seanheiney/boss/services/sandbox-service/internal/job"
	"github.com/seanheiney/boss/services/sandbox-service/internal/run"
)

func requireProcessBackend(t *testing.T) {
	t.Helper()
	for _, bin := range []string{"python3", "git"} {
		if _, err := exec.LookPath(bin); err != nil {
			t.Skipf("%s not installed", bin)
		}
	}
}

func liveServer(t *testing.T, frozen bool) *Server {
	t.Helper()
	requireProcessBackend(t)
	store := job.NewStore(frozen)
	b := &backend.Process{}
	store.ConfigureBackend(b.Name(), b.Check)
	runner := run.New(store, b, t.TempDir())
	return NewWithRunner(store, "tok", runner)
}

func pollUntilTerminal(t *testing.T, srv http.Handler, id string, budget time.Duration) contract.StatusResponse {
	t.Helper()
	deadline := time.Now().Add(budget)
	for time.Now().Before(deadline) {
		res := doJSON(t, srv, http.MethodGet, "/v1/sandbox/tasks/"+id, nil, "tok")
		if res.StatusCode != 200 {
			t.Fatalf("poll status=%d", res.StatusCode)
		}
		var status contract.StatusResponse
		if err := json.NewDecoder(res.Body).Decode(&status); err != nil {
			t.Fatal(err)
		}
		if contract.IsTerminal(status.Status) {
			return status
		}
		time.Sleep(150 * time.Millisecond)
	}
	t.Fatalf("task %s did not reach a terminal status in %s", id, budget)
	return contract.StatusResponse{}
}

// The whole lane, for real: submit → queued → running → done with the guest's
// result payload, then fs/logs inspection and a success release.
func TestProcessBackendRunsEchoTaskEndToEnd(t *testing.T) {
	srv := liveServer(t, false)
	body, _ := json.Marshal(map[string]any{
		"contract_version": "1.0",
		"task_id":          "crun_live_echo",
		"tenant_id":        "default",
		"engine":           "echo",
		"test_mode":        true,
		"engine_env":       map[string]string{},
		"repo":             map[string]string{},
		"instructions":     "echo round-trip",
		"timeout":          120,
	})
	res := doJSON(t, srv, http.MethodPost, "/v1/sandbox/tasks", body, "tok")
	if res.StatusCode != http.StatusAccepted {
		t.Fatalf("submit status=%d", res.StatusCode)
	}
	var submitted contract.SubmitResponse
	if err := json.NewDecoder(res.Body).Decode(&submitted); err != nil {
		t.Fatal(err)
	}
	if submitted.Status != contract.StatusQueued {
		t.Fatalf("submit status=%q", submitted.Status)
	}

	status := pollUntilTerminal(t, srv, submitted.SandboxTaskID, 60*time.Second)
	if status.Status != contract.StatusDone {
		t.Fatalf("status=%s error=%s", status.Status, status.Error)
	}
	if status.Backend != "process" || status.FirecrackerLive {
		t.Fatalf("backend honesty: %+v", status)
	}
	if status.Result == nil || status.Result.Status != "ok" {
		t.Fatalf("result=%+v", status.Result)
	}
	found := false
	for _, f := range status.Result.Diff {
		if f.Path == "SANDBOX_OK.txt" {
			found = true
		}
	}
	if !found {
		t.Fatalf("echo diff must contain SANDBOX_OK.txt: %+v", status.Result.Diff)
	}
	if status.Result.DiffSummary == nil || status.Result.DiffSummary.FilesChanged < 1 {
		t.Fatalf("diff_summary missing: %+v", status.Result.DiffSummary)
	}

	// fs browse of the retained workspace (the console's root-cause path).
	fsRes := doJSON(t, srv, http.MethodGet,
		"/v1/sandbox/tasks/"+submitted.SandboxTaskID+"/fs?path=/work/repo", nil, "tok")
	if fsRes.StatusCode != 200 {
		t.Fatalf("fs status=%d", fsRes.StatusCode)
	}
	var tree struct {
		Entries []struct {
			Name string `json:"name"`
			Dir  bool   `json:"dir"`
		} `json:"entries"`
	}
	if err := json.NewDecoder(fsRes.Body).Decode(&tree); err != nil {
		t.Fatal(err)
	}
	hasMarker := false
	for _, e := range tree.Entries {
		if e.Name == "SANDBOX_OK.txt" && !e.Dir {
			hasMarker = true
		}
	}
	if !hasMarker {
		t.Fatalf("fs must list the marker file: %+v", tree.Entries)
	}
	fileRes := doJSON(t, srv, http.MethodGet,
		"/v1/sandbox/tasks/"+submitted.SandboxTaskID+"/fs?path=/work/repo/SANDBOX_OK.txt", nil, "tok")
	var fileBody struct {
		Content string `json:"content"`
	}
	if err := json.NewDecoder(fileRes.Body).Decode(&fileBody); err != nil {
		t.Fatal(err)
	}
	if fileBody.Content == "" {
		t.Fatal("fs file read returned empty content")
	}
	escape := doJSON(t, srv, http.MethodGet,
		"/v1/sandbox/tasks/"+submitted.SandboxTaskID+"/fs?path=/work/../../etc", nil, "tok")
	if escape.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("path escape must be refused: %d", escape.StatusCode)
	}

	logsRes := doJSON(t, srv, http.MethodGet,
		"/v1/sandbox/tasks/"+submitted.SandboxTaskID+"/logs", nil, "tok")
	if logsRes.StatusCode != 200 {
		t.Fatalf("logs status=%d", logsRes.StatusCode)
	}

	rel := doJSON(t, srv, http.MethodDelete, "/v1/sandbox/tasks/"+submitted.SandboxTaskID, nil, "tok")
	if rel.StatusCode != 200 {
		t.Fatalf("release status=%d", rel.StatusCode)
	}
	gone := doJSON(t, srv, http.MethodGet, "/v1/sandbox/tasks/"+submitted.SandboxTaskID, nil, "tok")
	if gone.StatusCode != http.StatusNotFound {
		t.Fatalf("released task must 404: %d", gone.StatusCode)
	}
}

// Coding submits stay 422 microvm_claims_frozen even with a LIVE backend: the
// freeze is the control-plane honesty gate, not a missing implementation.
func TestLiveBackendStillRespectsClaimsFreeze(t *testing.T) {
	srv := liveServer(t, true)
	res := doJSON(t, srv, http.MethodPost, "/v1/sandbox/tasks", goldenSubmit(t), "tok")
	if res.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("status=%d", res.StatusCode)
	}
	var body contract.ErrorBody
	if err := json.NewDecoder(res.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body.Reason != contract.ReasonClaimsFrozen {
		t.Fatalf("reason=%s", body.Reason)
	}
}

func TestLiveBackendHealthReportsReady(t *testing.T) {
	srv := liveServer(t, false)
	res := doJSON(t, srv, http.MethodGet, "/v1/sandbox/health", nil, "")
	var body map[string]any
	if err := json.NewDecoder(res.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body["ok"] != true || body["execution_ready"] != true {
		t.Fatalf("live process backend should be ready: %v", body)
	}
	if body["backend"] != "process" || body["firecracker_live"] != false {
		t.Fatalf("backend honesty: %v", body)
	}
}

func TestCancelLandsTerminalCancelled(t *testing.T) {
	srv := liveServer(t, false)
	// A task that sleeps long enough to be cancelled mid-flight: echo guest in
	// test_mode runs quickly, so use its own test_commands to stall.
	body, _ := json.Marshal(map[string]any{
		"contract_version": "1.0",
		"task_id":          "crun_cancel_me",
		"engine":           "echo",
		"test_mode":        true,
		"engine_env":       map[string]string{},
		"repo":             map[string]string{},
		"test_commands":    []string{"python3 -c 'import time; time.sleep(300)'"},
		"timeout":          600,
	})
	res := doJSON(t, srv, http.MethodPost, "/v1/sandbox/tasks", body, "tok")
	if res.StatusCode != http.StatusAccepted {
		t.Fatalf("submit status=%d", res.StatusCode)
	}
	var submitted contract.SubmitResponse
	if err := json.NewDecoder(res.Body).Decode(&submitted); err != nil {
		t.Fatal(err)
	}
	time.Sleep(300 * time.Millisecond)
	cancelRes := doJSON(t, srv, http.MethodPost,
		"/v1/sandbox/tasks/"+submitted.SandboxTaskID+"/cancel",
		[]byte(`{"reason":"operator_stop"}`), "tok")
	if cancelRes.StatusCode != 200 {
		t.Fatalf("cancel status=%d", cancelRes.StatusCode)
	}
	status := pollUntilTerminal(t, srv, submitted.SandboxTaskID, 20*time.Second)
	if status.Status != contract.StatusCancelled {
		t.Fatalf("status=%s", status.Status)
	}
}

// The shared secret is accepted both as X-Sandbox-Token and as a Bearer token.
func TestBearerAuthAccepted(t *testing.T) {
	srv := New(job.NewStore(true), "secret")
	req := httptest.NewRequest(http.MethodPost, "/v1/sandbox/tasks",
		bytes.NewReader(goldenSubmit(t)))
	req.Header.Set("Authorization", "Bearer secret")
	rec := httptest.NewRecorder()
	srv.ServeHTTP(rec, req)
	if rec.Result().StatusCode == http.StatusUnauthorized {
		t.Fatal("Bearer token must authorize")
	}
}
