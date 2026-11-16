package fakecp

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/seanventures/boss/services/sandbox-service/internal/hostapi"
)

// The work side of the fake (LLD §3.13.0, REVISION): one hosts/next per host
// with {wait_ms, state_gen}; BOSS's own replica model (total slots from the
// replica, busy = active leases); resync on a generation mismatch; claims to
// the serving replica with the most free slots; hand-back re-queues with a new
// epoch.

type workRun struct {
	RunID, JobID, Deployment, Holder, Token string
	Epoch                                   int64
}

// Claim is one assignment the fake handed out.
type Claim struct {
	hostapi.Assignment
	At time.Time
}

type workState struct {
	mu          sync.Mutex
	queue       []*workRun
	leased      map[string]*workRun
	storedGen   map[string]int64
	haveGen     map[string]bool
	replicas    map[string]map[string]hostapi.ReplicaStatus // host → replica → status
	inflight    map[string]int
	maxInflight map[string]int
	nextCalls   map[string]int
	resyncs     map[string]int
	puts        map[string]int
	bodyKeys    map[string]bool
	claims      []Claim
	handBacks   []string
	prefer      string
	rr          int
	wake        chan struct{}
	// SlotsOverride, if > 0, is BOSS's idea of every replica's total slots.
	slotsOverride int
}

func (w *workState) init() {
	if w.leased == nil {
		w.leased = map[string]*workRun{}
		w.storedGen = map[string]int64{}
		w.haveGen = map[string]bool{}
		w.replicas = map[string]map[string]hostapi.ReplicaStatus{}
		w.inflight = map[string]int{}
		w.maxInflight = map[string]int{}
		w.nextCalls = map[string]int{}
		w.resyncs = map[string]int{}
		w.puts = map[string]int{}
		w.bodyKeys = map[string]bool{}
		w.wake = make(chan struct{})
	}
}

func (w *workState) notifyLocked() {
	close(w.wake)
	w.wake = make(chan struct{})
}

// ---- test controls -------------------------------------------------------

// Enqueue admits a run for a deployment.
func (cp *CP) Enqueue(deployment, runID string) {
	w := &cp.work
	w.mu.Lock()
	defer w.mu.Unlock()
	w.init()
	w.queue = append(w.queue, &workRun{RunID: runID, JobID: "job_" + runID, Deployment: deployment})
	w.notifyLocked()
}

// PreferReplica makes the next claim go to this replica if it is eligible.
func (cp *CP) PreferReplica(id string) {
	cp.work.mu.Lock()
	cp.work.init()
	cp.work.prefer = id
	cp.work.mu.Unlock()
}

// SetSlotsOverride makes BOSS believe every replica has n slots.
func (cp *CP) SetSlotsOverride(n int) {
	cp.work.mu.Lock()
	cp.work.slotsOverride = n
	cp.work.mu.Unlock()
}

// ForgetGen makes the next hosts/next answer {resync:true}.
func (cp *CP) ForgetGen(host string) {
	cp.work.mu.Lock()
	cp.work.init()
	cp.work.haveGen[host] = false
	cp.work.mu.Unlock()
}

// WorkStats is a snapshot of the work-protocol counters for one host.
type WorkStats struct {
	Inflight, MaxInflight, NextCalls, Resyncs, Puts int
	BodyKeys                                        []string
	Claims                                          []Claim
	HandBacks                                       []string
	Queued                                          int
	ReplicasKnown                                   int
}

func (cp *CP) Work(host string) WorkStats {
	w := &cp.work
	w.mu.Lock()
	defer w.mu.Unlock()
	w.init()
	var keys []string
	for k := range w.bodyKeys {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return WorkStats{Inflight: w.inflight[host], MaxInflight: w.maxInflight[host], NextCalls: w.nextCalls[host],
		Resyncs: w.resyncs[host], Puts: w.puts[host], BodyKeys: keys,
		Claims: append([]Claim(nil), w.claims...), HandBacks: append([]string(nil), w.handBacks...),
		Queued: len(w.queue), ReplicasKnown: len(w.replicas[host])}
}

// ---- protocol ------------------------------------------------------------

func (cp *CP) observeReplicas(host string, rs []hostapi.ReplicaStatus, gen int64, full bool) {
	w := &cp.work
	w.mu.Lock()
	defer w.mu.Unlock()
	w.init()
	if full || w.replicas[host] == nil {
		w.replicas[host] = map[string]hostapi.ReplicaStatus{}
	}
	for _, r := range rs {
		if r.ReplicaID != "" {
			w.replicas[host][r.ReplicaID] = r
		}
	}
	if gen >= 0 {
		w.storedGen[host], w.haveGen[host] = gen, true
	}
	w.notifyLocked()
}

func (cp *CP) putReplicas(w http.ResponseWriter, r *http.Request, host string) {
	var m hostapi.ReplicaMap
	if err := json.NewDecoder(io.LimitReader(r.Body, 8<<20)).Decode(&m); err != nil {
		http.Error(w, "bad body", http.StatusBadRequest)
		return
	}
	cp.work.mu.Lock()
	cp.work.init()
	cp.work.puts[host]++
	cp.work.mu.Unlock()
	cp.observeReplicas(host, m.Replicas, m.StateGen, true)
	writeJSON(w, http.StatusOK, map[string]any{"host_id": host, "state_gen": m.StateGen})
}

func (cp *CP) hostsNext(w http.ResponseWriter, r *http.Request) {
	host, ok := cp.authHost(w, r, "")
	if !ok {
		return
	}
	raw, _ := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	var keys map[string]json.RawMessage
	_ = json.Unmarshal(raw, &keys)
	var req hostapi.HostsNextRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		http.Error(w, "bad body", http.StatusBadRequest)
		return
	}
	ws := &cp.work
	ws.mu.Lock()
	ws.init()
	for k := range keys {
		ws.bodyKeys[k] = true
	}
	ws.nextCalls[host]++
	ws.inflight[host]++
	if ws.inflight[host] > ws.maxInflight[host] {
		ws.maxInflight[host] = ws.inflight[host]
	}
	ws.mu.Unlock()
	defer func() {
		ws.mu.Lock()
		ws.inflight[host]--
		ws.mu.Unlock()
	}()
	wait := time.Duration(req.WaitMS) * time.Millisecond
	if wait > 30*time.Second {
		wait = 30 * time.Second
	}
	deadline := time.NewTimer(wait)
	defer deadline.Stop()
	for {
		ws.mu.Lock()
		if !ws.haveGen[host] || ws.storedGen[host] != req.StateGen {
			ws.resyncs[host]++
			ws.mu.Unlock()
			writeJSON(w, http.StatusOK, hostapi.HostsNextResponse{Resync: true})
			return
		}
		runs := ws.claimLocked(host)
		ch := ws.wake
		ws.mu.Unlock()
		if len(runs) > 0 {
			if cp.OnClaim != nil {
				for _, a := range runs {
					cp.OnClaim(a)
				}
			}
			writeJSON(w, http.StatusOK, hostapi.HostsNextResponse{Runs: runs})
			return
		}
		select {
		case <-ch:
		case <-deadline.C:
			writeJSON(w, http.StatusOK, hostapi.HostsNextResponse{Runs: []hostapi.Assignment{}})
			return
		case <-r.Context().Done():
			return
		}
	}
}

// claimLocked assigns queued runs to this host's serving replicas: the one
// with the most total−busy (busy = active leases), round robin on ties.
func (w *workState) claimLocked(host string) []hostapi.Assignment {
	var out []hostapi.Assignment
	busy := map[string]int{}
	for _, run := range w.leased {
		busy[run.Holder]++
	}
	var rest []*workRun
	for _, run := range w.queue {
		var cands []hostapi.ReplicaStatus
		for _, rs := range w.replicas[host] {
			total := rs.Slots
			if w.slotsOverride > 0 {
				total = w.slotsOverride
			}
			if rs.State == hostapi.StateServing && rs.DeploymentID == run.Deployment && total-busy[rs.ReplicaID] > 0 {
				cands = append(cands, rs)
			}
		}
		if len(cands) == 0 {
			rest = append(rest, run)
			continue
		}
		sort.Slice(cands, func(i, j int) bool { return cands[i].ReplicaID < cands[j].ReplicaID })
		pick := cands[w.rr%len(cands)]
		w.rr++
		best := -1
		for _, c := range cands {
			total := c.Slots
			if w.slotsOverride > 0 {
				total = w.slotsOverride
			}
			if f := total - busy[c.ReplicaID]; f > best {
				best, pick = f, c
			}
		}
		if w.prefer != "" {
			for _, c := range cands {
				if c.ReplicaID == w.prefer {
					pick = c
				}
			}
			w.prefer = ""
		}
		run.Epoch++
		run.Holder = pick.ReplicaID
		tok := make([]byte, 8)
		_, _ = rand.Read(tok)
		run.Token = "lease_" + hex.EncodeToString(tok)
		w.leased[run.RunID] = run
		busy[pick.ReplicaID]++
		a := hostapi.Assignment{RunID: run.RunID, JobID: run.JobID, ReplicaID: pick.ReplicaID, Epoch: run.Epoch, RunToken: run.Token}
		w.claims = append(w.claims, Claim{Assignment: a, At: time.Now()})
		out = append(out, a)
	}
	w.queue = rest
	return out
}

// runLease handles POST /v1/run-plane/runs/{run}/lease {"action":"hand_back"}.
func (cp *CP) runLease(w http.ResponseWriter, r *http.Request, runID string) {
	if _, ok := cp.authHost(w, r, ""); !ok {
		return
	}
	var body hostapi.LeaseAction
	_ = json.NewDecoder(io.LimitReader(r.Body, 1<<16)).Decode(&body)
	ws := &cp.work
	ws.mu.Lock()
	defer ws.mu.Unlock()
	ws.init()
	run, ok := ws.leased[runID]
	epoch, _ := strconv.ParseInt(r.Header.Get("X-Boss-Fence-Epoch"), 10, 64)
	if !ok || run.Holder != r.Header.Get("X-Boss-Replica") || run.Epoch != epoch || run.Token != r.Header.Get("X-Boss-Run-Token") {
		writeJSON(w, http.StatusConflict, map[string]any{"detail": map[string]string{"error": "fenced", "message": "not the lease holder"}})
		return
	}
	if body.HandBack {
		body.Action = "hand_back"
	}
	switch body.Action {
	case "hand_back":
		delete(ws.leased, runID)
		run.Holder, run.Token = "", ""
		ws.queue = append([]*workRun{run}, ws.queue...)
		ws.handBacks = append(ws.handBacks, runID)
		ws.notifyLocked()
		writeJSON(w, http.StatusOK, map[string]any{"handed_back": true})
	default:
		writeJSON(w, http.StatusBadRequest, map[string]any{"detail": map[string]string{"error": "bad_action"}})
	}
}

// routeRunPlane serves the work endpoints and hands the rest to RunPlane.
func (cp *CP) routeRunPlane(w http.ResponseWriter, r *http.Request) bool {
	switch {
	case r.URL.Path == hostapi.PathHostsNext && r.Method == http.MethodPost:
		cp.hostsNext(w, r)
		return true
	case strings.HasPrefix(r.URL.Path, "/v1/run-plane/runs/") && strings.HasSuffix(r.URL.Path, "/tokens") && r.Method == http.MethodPost:
		cp.runTokens(w, r)
		return true
	case strings.HasPrefix(r.URL.Path, "/v1/run-plane/runs/") && strings.HasSuffix(r.URL.Path, "/lease") && r.Method == http.MethodPost:
		run := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/v1/run-plane/runs/"), "/lease")
		cp.runLease(w, r, run)
		return true
	}
	return false
}

// SetTokensRefused makes POST …/tokens answer 409 (the identity service
// refused the run's tokens), for hand-back tests (P8b S13).
func (cp *CP) SetTokensRefused(refuse bool) { cp.tokensRefused.Store(refuse) }

// TokenMints is how many run-token mints the host asked for.
func (cp *CP) TokenMints() int64 { return cp.tokenMints.Load() }

// runTokens answers POST /v1/run-plane/runs/{run}/tokens like the control
// plane: one opaque token per audience for the host's jkt (HOST_API.md §4.8).
func (cp *CP) runTokens(w http.ResponseWriter, r *http.Request) {
	if _, ok := cp.authHost(w, r, ""); !ok {
		return
	}
	cp.tokenMints.Add(1)
	if cp.tokensRefused.Load() {
		writeJSON(w, http.StatusConflict, map[string]any{"detail": map[string]string{
			"error": "token_grant_inactive", "message": "the identity service refused this run's tokens"}})
		return
	}
	var req hostapi.RunTokensRequest
	_ = json.NewDecoder(io.LimitReader(r.Body, 1<<16)).Decode(&req)
	run := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/v1/run-plane/runs/"), "/tokens")
	out := hostapi.RunTokensResponse{RunID: run, JKT: req.JKT, ExpiresIn: 300, RotateAfterSeconds: 200,
		Tokens: map[string]hostapi.IssuedToken{}}
	for _, aud := range req.Audiences {
		out.Tokens[aud] = hostapi.IssuedToken{AccessToken: "fake." + aud + "." + run, TokenType: "DPoP", ExpiresIn: 300}
	}
	writeJSON(w, http.StatusOK, out)
}
