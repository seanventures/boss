package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
)

// RunLease is what the HOST holds for a run its VM is driving (HOST_API.md
// §4.5): the run token never enters the VM.
type RunLease struct {
	ReplicaID string
	JobID     string
	Token     string
	Epoch     int64
}

// RunMap is the host's in-memory run → lease map (never bbolt, never a log).
type RunMap struct {
	mu sync.Mutex
	m  map[string]RunLease
}

func NewRunMap() *RunMap { return &RunMap{m: map[string]RunLease{}} }

func (r *RunMap) Put(runID string, l RunLease) {
	r.mu.Lock()
	r.m[runID] = l
	r.mu.Unlock()
}

func (r *RunMap) Get(runID string) (RunLease, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	l, ok := r.m[runID]
	return l, ok
}

func (r *RunMap) Delete(runID string) {
	r.mu.Lock()
	delete(r.m, runID)
	r.mu.Unlock()
}

func (r *RunMap) Len() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.m)
}

// runOfPath returns the run id of a /v1/run-plane/runs/{run}/… path.
func runOfPath(p string) string {
	rest, ok := strings.CutPrefix(p, "/v1/run-plane/runs/")
	if !ok {
		return ""
	}
	id, _, _ := strings.Cut(rest, "/")
	return id
}

// leaseEnds reports whether a lease call ends the run's lease on success
// (settle, fail, hand back), reading and restoring a small body.
func leaseEnds(r *http.Request) bool {
	if !strings.HasSuffix(r.URL.Path, "/lease") || r.Body == nil {
		return false
	}
	raw, _ := io.ReadAll(io.LimitReader(r.Body, 64<<10))
	r.Body = io.NopCloser(bytes.NewReader(raw))
	var b struct {
		Action   string `json:"action"`
		HandBack bool   `json:"hand_back"`
	}
	_ = json.Unmarshal(raw, &b)
	return b.HandBack || b.Action == "settle" || b.Action == "fail" || b.Action == "hand_back"
}

// stripAdmitToken removes lease.run_token from an admit response, recording
// the lease in the run map for the admitting replica.
func (g *Gateway) stripAdmitToken(resp *http.Response, replicaID string) error {
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return nil
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	resp.Body.Close()
	if err != nil {
		return err
	}
	var doc map[string]any
	if json.Unmarshal(raw, &doc) == nil {
		if lease, ok := doc["lease"].(map[string]any); ok {
			if tok, _ := lease["run_token"].(string); tok != "" {
				runID, _ := doc["run_id"].(string)
				l := RunLease{ReplicaID: replicaID, Token: tok}
				l.JobID, _ = lease["job_id"].(string)
				switch e := lease["fence_epoch"].(type) {
				case float64:
					l.Epoch = int64(e)
				case string:
					l.Epoch, _ = strconv.ParseInt(e, 10, 64)
				}
				if runID != "" {
					g.Runs.Put(runID, l)
					// S13: the admitted run's tokens exist before the guest learns
					// of the run, so its next call already carries them. A refused
					// mint answers the guest 503 tokens_unavailable (the lease then
					// expires and BOSS re-queues the run); a control plane that
					// predates S13 (404) leaves the run on the host proof alone.
					if g.Keys != nil {
						ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
						err := g.Keys.Ensure(ctx, hostapi.Assignment{RunID: runID, JobID: l.JobID,
							ReplicaID: replicaID, Epoch: l.Epoch, RunToken: tok})
						cancel()
						if err != nil && !g.mintNotSupported(err) {
							g.Runs.Delete(runID)
							g.Keys.Forget(runID)
							body, _ := json.Marshal(map[string]string{"error": "boss guest gateway: tokens_unavailable",
								"reason": "tokens_unavailable"})
							resp.StatusCode, resp.Status = http.StatusServiceUnavailable, "503 Service Unavailable"
							resp.Header.Set("Content-Type", "application/json")
							resp.Body = io.NopCloser(bytes.NewReader(body))
							resp.ContentLength = int64(len(body))
							resp.Header.Set("Content-Length", strconv.Itoa(len(body)))
							return nil
						}
					}
				}
				delete(lease, "run_token")
				if out, err := json.Marshal(doc); err == nil {
					raw = out
				}
			}
		}
	}
	resp.Body = io.NopCloser(bytes.NewReader(raw))
	resp.ContentLength = int64(len(raw))
	resp.Header.Set("Content-Length", strconv.Itoa(len(raw)))
	return nil
}

// mintNotSupported reports a mint refused because the control plane does not
// serve the tokens endpoint (404): it predates P8b S13.
func (g *Gateway) mintNotSupported(err error) bool {
	if g.MintNotSupported != nil {
		return g.MintNotSupported(err)
	}
	return false
}
