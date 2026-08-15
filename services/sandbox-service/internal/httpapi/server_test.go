package httpapi

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/seanheiney/boss/services/sandbox-service/internal/contract"
	"github.com/seanheiney/boss/services/sandbox-service/internal/job"
)

func goldenSubmit(t *testing.T) []byte {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("caller")
	}
	path := filepath.Join(filepath.Dir(file), "..", "..", "testdata", "submit_valid.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func doJSON(t *testing.T, srv http.Handler, method, path string, body []byte, token string) *http.Response {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		rdr = bytes.NewReader(body)
	}
	req := httptest.NewRequest(method, path, rdr)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if token != "" {
		req.Header.Set("X-Sandbox-Token", token)
	}
	rec := httptest.NewRecorder()
	srv.ServeHTTP(rec, req)
	return rec.Result()
}

func TestHealthNeverClaimsFirecrackerWhenFrozen(t *testing.T) {
	srv := New(job.NewStore(true), "")
	res := doJSON(t, srv, http.MethodGet, "/v1/sandbox/health", nil, "")
	if res.StatusCode != 200 {
		t.Fatalf("status=%d", res.StatusCode)
	}
	var body map[string]any
	if err := json.NewDecoder(res.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body["ok"] != false {
		t.Fatalf("ok=%v — must not invent host.connected=true", body["ok"])
	}
	if body["firecracker_live"] != false {
		t.Fatalf("firecracker_live=%v", body["firecracker_live"])
	}
	if body["execution_ready"] != false {
		t.Fatalf("execution_ready=%v", body["execution_ready"])
	}
	if body["claims_frozen"] != true {
		t.Fatalf("claims_frozen=%v", body["claims_frozen"])
	}
	if body["reason"] != contract.ReasonClaimsFrozen {
		t.Fatalf("reason=%v", body["reason"])
	}
}

func TestSubmitCodingFrozenIs422NamedReason(t *testing.T) {
	srv := New(job.NewStore(true), "tok")
	res := doJSON(t, srv, http.MethodPost, "/v1/sandbox/tasks", goldenSubmit(t), "tok")
	if res.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("status=%d", res.StatusCode)
	}
	var body contract.ErrorBody
	if err := json.NewDecoder(res.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body.Reason != contract.ReasonClaimsFrozen || body.Code != contract.ReasonClaimsFrozen {
		t.Fatalf("body=%+v", body)
	}
	if body.Error == "" {
		t.Fatal("error must not be empty")
	}
}

func TestSubmitCodingUnfrozenWithoutFirecrackerIs422(t *testing.T) {
	srv := New(job.NewStore(false), "")
	res := doJSON(t, srv, http.MethodPost, "/v1/sandbox/tasks", goldenSubmit(t), "")
	if res.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("status=%d", res.StatusCode)
	}
	var body contract.ErrorBody
	if err := json.NewDecoder(res.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body.Reason != contract.ReasonFirecrackerDisabled {
		t.Fatalf("reason=%s", body.Reason)
	}
}

func TestEchoSubmitPollHeartbeatWhileFrozen(t *testing.T) {
	srv := New(job.NewStore(true), "tok")
	echo, _ := json.Marshal(map[string]any{
		"contract_version": "1.0",
		"task_id":          "exercise_echo_api",
		"engine":           "echo",
		"test_mode":        true,
		"engine_env":       map[string]string{},
		"repo":             map[string]string{},
	})
	res := doJSON(t, srv, http.MethodPost, "/v1/sandbox/tasks", echo, "tok")
	if res.StatusCode != http.StatusAccepted {
		t.Fatalf("submit status=%d", res.StatusCode)
	}
	var submitted contract.SubmitResponse
	if err := json.NewDecoder(res.Body).Decode(&submitted); err != nil {
		t.Fatal(err)
	}
	if submitted.SandboxTaskID == "" {
		t.Fatal("missing sandbox_task_id")
	}

	poll := doJSON(t, srv, http.MethodGet, "/v1/sandbox/tasks/"+submitted.SandboxTaskID, nil, "tok")
	if poll.StatusCode != 200 {
		t.Fatalf("poll status=%d", poll.StatusCode)
	}
	var status contract.StatusResponse
	if err := json.NewDecoder(poll.Body).Decode(&status); err != nil {
		t.Fatal(err)
	}
	if status.Status != contract.StatusDone {
		t.Fatalf("status=%s", status.Status)
	}
	if status.FirecrackerLive || status.CodingClaimed {
		t.Fatalf("echo must not claim firecracker/coding: %+v", status)
	}
	if status.Lease == nil || status.Lease.TTLSeconds <= 0 {
		t.Fatalf("lease missing: %+v", status.Lease)
	}
	if len(status.Result.Diff) != 1 || status.Result.Diff[0].Path != "SANDBOX_OK.txt" {
		t.Fatalf("result=%+v", status.Result)
	}

	hb := doJSON(t, srv, http.MethodPost, "/v1/sandbox/tasks/"+submitted.SandboxTaskID+"/heartbeat", []byte(`{}`), "tok")
	if hb.StatusCode != 200 {
		t.Fatalf("heartbeat status=%d", hb.StatusCode)
	}
}

func TestPreviewFrozenIs422(t *testing.T) {
	srv := New(job.NewStore(true), "")
	res := doJSON(t, srv, http.MethodPost, "/v1/sandbox/tasks/sbx_x/preview", []byte(`{"ttl_seconds":60}`), "")
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

func TestMissingTokenRejected(t *testing.T) {
	srv := New(job.NewStore(true), "secret")
	res := doJSON(t, srv, http.MethodPost, "/v1/sandbox/tasks", goldenSubmit(t), "")
	if res.StatusCode != http.StatusUnauthorized {
		t.Fatalf("status=%d", res.StatusCode)
	}
}

func TestMissingTaskIs404Named(t *testing.T) {
	srv := New(job.NewStore(true), "")
	res := doJSON(t, srv, http.MethodGet, "/v1/sandbox/tasks/sbx_missing", nil, "")
	if res.StatusCode != http.StatusNotFound {
		t.Fatalf("status=%d", res.StatusCode)
	}
	var body contract.ErrorBody
	if err := json.NewDecoder(res.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body.Reason != contract.ReasonTaskMissing {
		t.Fatalf("reason=%s", body.Reason)
	}
}
