// Package fakecp is a Go fake of the BOSS control plane's host API
// (docs/infra/HOST_API.md). It enforces the same auth rules the real one must:
// one-time join tokens, a CA it signs with, pinned host certificate
// fingerprints, revocation, and level-based long-poll desired state. Tests and
// the boss-fakecp binary (for boss-1 runs) use it; production never does.
package fakecp

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/seanventures/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanventures/boss/services/sandbox-service/internal/pki"
)

type host struct {
	id         string
	cert       *x509.Certificate // the pinned certificate (the latest issued)
	revoked    bool
	heartbeats int
	last       *hostapi.HeartbeatRequest
	lastAt     time.Time
	observed   []hostapi.ReplicaStatus
}

// CP is the fake control plane.
type CP struct {
	CA          *pki.CA
	TrustDomain string
	CertTTL     time.Duration // client cert + intermediate lifetime (default 24 h)

	mu         sync.Mutex
	changed    chan struct{} // closed and replaced on every desired change
	joinTokens map[string]bool
	hosts      map[string]*host
	desired    map[string]*hostapi.DesiredState
	calls      []hostapi.GuestCall
	callIDs    map[string]bool
	events     []hostapi.HostEvent
	nextHost   int
	refusals   map[string]int    // reason → count (for assertions)
	byFP       map[string]string // pinned cert fingerprint → host id
	jtis       map[string]bool   // spent proof jtis
	// NoEvents makes /events 404, like a control plane without it.
	NoEvents bool
	// RunPlane, if set, serves /v1/run-plane/* (host-authenticated) for tests.
	RunPlane http.Handler

	tokensRefused atomic.Bool
	tokenMints    atomic.Int64
	// OnClaim, if set, runs after each claim and before the answer is sent.
	OnClaim func(hostapi.Assignment)
	work    workState

	srv      *http.Server
	listener net.Listener
	URL      string
}

// New makes a fake control plane with a fresh CA.
func New() (*CP, error) {
	ca, err := pki.NewCA("boss-fake-root")
	if err != nil {
		return nil, err
	}
	return &CP{
		CA: ca, TrustDomain: "test-install.boss", CertTTL: 24 * time.Hour,
		changed: make(chan struct{}), joinTokens: map[string]bool{},
		hosts: map[string]*host{}, desired: map[string]*hostapi.DesiredState{},
		refusals: map[string]int{}, byFP: map[string]string{}, jtis: map[string]bool{},
		callIDs: map[string]bool{},
	}, nil
}

// Start serves TLS on addr ("127.0.0.1:0" for tests). extraNames are added to
// the server certificate (e.g. the host's LAN IP for a boss-1 run).
func (cp *CP) Start(addr string, extraNames ...string) error {
	names := append([]string{"127.0.0.1", "localhost"}, extraNames...)
	cert, err := cp.CA.ServerCert(names...)
	if err != nil {
		return err
	}
	pool := x509.NewCertPool()
	pool.AddCert(cp.CA.Cert)
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return err
	}
	cp.listener = ln
	cp.srv = &http.Server{
		Handler: cp.Handler(),
		TLSConfig: &tls.Config{
			Certificates: []tls.Certificate{cert},
			ClientCAs:    pool,
			ClientAuth:   tls.VerifyClientCertIfGiven, // the proof is the credential; a cert is optional
			MinVersion:   tls.VersionTLS12,
		},
	}
	cp.URL = "https://" + ln.Addr().String()
	go func() { _ = cp.srv.ServeTLS(ln, "", "") }()
	return nil
}

func (cp *CP) Close() {
	if cp.srv != nil {
		_ = cp.srv.Close()
	}
}

// ---- Test controls -------------------------------------------------------

// AddJoinToken registers a one-time join token.
func (cp *CP) AddJoinToken(tok string) {
	cp.mu.Lock()
	cp.joinTokens[tok] = true
	cp.mu.Unlock()
}

// Revoke marks a host revoked: every later call from it is refused 403.
func (cp *CP) Revoke(hostID string) {
	cp.mu.Lock()
	if h, ok := cp.hosts[hostID]; ok {
		h.revoked = true
	}
	cp.mu.Unlock()
}

// SetDesired replaces a host's desired state and bumps its generation.
func (cp *CP) SetDesired(hostID string, ds hostapi.DesiredState) int64 {
	cp.mu.Lock()
	defer cp.mu.Unlock()
	var gen int64 = 1
	if cur, ok := cp.desired[hostID]; ok {
		gen = cur.Generation + 1
	}
	ds.HostID = hostID
	ds.Generation = gen
	if ds.Endpoints == nil {
		ds.Endpoints = map[string]string{}
	}
	if _, ok := ds.Endpoints["run_plane"]; !ok {
		ds.Endpoints["run_plane"] = "/v1/run-plane"
	}
	cp.desired[hostID] = &ds
	close(cp.changed)
	cp.changed = make(chan struct{})
	return gen
}

// Desired returns a copy of the host's desired state.
func (cp *CP) Desired(hostID string) hostapi.DesiredState {
	cp.mu.Lock()
	defer cp.mu.Unlock()
	if d, ok := cp.desired[hostID]; ok {
		return *d
	}
	return hostapi.DesiredState{HostID: hostID}
}

// LastHeartbeat returns the most recent accepted heartbeat (nil if none).
func (cp *CP) LastHeartbeat(hostID string) (*hostapi.HeartbeatRequest, time.Time) {
	cp.mu.Lock()
	defer cp.mu.Unlock()
	if h, ok := cp.hosts[hostID]; ok && h.last != nil {
		cp := *h.last
		return &cp, h.lastAt
	}
	return nil, time.Time{}
}

// HeartbeatCount is the number of accepted heartbeats.
func (cp *CP) HeartbeatCount(hostID string) int {
	cp.mu.Lock()
	defer cp.mu.Unlock()
	if h, ok := cp.hosts[hostID]; ok {
		return h.heartbeats
	}
	return 0
}

// Refusals returns how many times a reason was returned.
func (cp *CP) Refusals(reason string) int {
	cp.mu.Lock()
	defer cp.mu.Unlock()
	return cp.refusals[reason]
}

// Observed returns the last /observed push.
func (cp *CP) Observed(hostID string) []hostapi.ReplicaStatus {
	cp.mu.Lock()
	defer cp.mu.Unlock()
	if h, ok := cp.hosts[hostID]; ok {
		return append([]hostapi.ReplicaStatus(nil), h.observed...)
	}
	return nil
}

func (cp *CP) GuestCalls() []hostapi.GuestCall {
	cp.mu.Lock()
	defer cp.mu.Unlock()
	return append([]hostapi.GuestCall(nil), cp.calls...)
}

func (cp *CP) Events() []hostapi.HostEvent {
	cp.mu.Lock()
	defer cp.mu.Unlock()
	return append([]hostapi.HostEvent(nil), cp.events...)
}

// HostIDs lists enrolled hosts.
func (cp *CP) HostIDs() []string {
	cp.mu.Lock()
	defer cp.mu.Unlock()
	var out []string
	for id := range cp.hosts {
		out = append(out, id)
	}
	return out
}

// ---- HTTP ----------------------------------------------------------------

func (cp *CP) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc(hostapi.PathEnrol, cp.enrol)
	mux.HandleFunc("/v1/hosts/", cp.hostRoute)
	mux.HandleFunc("/v1/run-plane/", func(w http.ResponseWriter, r *http.Request) {
		if cp.routeRunPlane(w, r) {
			return
		}
		if cp.RunPlane == nil {
			http.NotFound(w, r)
			return
		}
		hostID, ok := cp.authHost(w, r, "")
		if !ok {
			return
		}
		r.Header.Set("X-Fake-Host", hostID)
		cp.RunPlane.ServeHTTP(w, r)
	})
	mux.HandleFunc("/admin/desired/", cp.adminDesired) // boss-fakecp only (localhost)
	mux.HandleFunc("/admin/revoke/", cp.adminRevoke)
	mux.HandleFunc("/admin/state", cp.adminState)
	return mux
}

// refuse answers in FastAPI's shape: {"detail": {"error": code, "message": msg}}.
func (cp *CP) refuse(w http.ResponseWriter, code int, reason, msg string) {
	cp.mu.Lock()
	cp.refusals[reason]++
	cp.mu.Unlock()
	writeJSON(w, code, map[string]any{"detail": map[string]string{"error": reason, "message": msg}})
}

func (cp *CP) enrol(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method", http.StatusMethodNotAllowed)
		return
	}
	var req hostapi.EnrolRequest
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&req); err != nil {
		cp.refuse(w, http.StatusUnprocessableEntity, "body_invalid", "bad body")
		return
	}
	cp.mu.Lock()
	ok := req.JoinToken != "" && cp.joinTokens[req.JoinToken]
	if ok {
		delete(cp.joinTokens, req.JoinToken) // one-time
	}
	cp.mu.Unlock()
	if !ok {
		cp.refuse(w, http.StatusUnauthorized, hostapi.ReasonJoinTokenInvalid, "that join token is not valid.")
		return
	}
	if req.Backend != "firecracker" && req.Backend != "process" {
		cp.refuse(w, http.StatusBadRequest, "backend_invalid", "backend must be firecracker or process.")
		return
	}
	cp.mu.Lock()
	cp.nextHost++
	id := fmt.Sprintf("hst_%d", cp.nextHost)
	prefix := fmt.Sprintf("10.200.%d.0/23", 4*cp.nextHost)
	cp.hosts[id] = &host{id: id}
	cp.mu.Unlock()
	resp, err := cp.issue(id, req.CSR, req.SVIDCSR)
	if err != nil {
		cp.refuse(w, http.StatusBadRequest, "csr_invalid", err.Error())
		return
	}
	resp.VMPrefix = prefix
	resp.SpiffeID = "spiffe://" + cp.TrustDomain + "/host/" + id
	resp.HeartbeatSeconds = 2
	resp.Endpoints = map[string]string{
		"heartbeat": "/v1/hosts/" + id + "/heartbeat", "desired": "/v1/hosts/" + id + "/desired",
		"observed": "/v1/hosts/" + id + "/observed", "guest_calls": "/v1/hosts/" + id + "/guest-calls",
		"renew": "/v1/hosts/" + id + "/renew", "run_plane": "/v1/run-plane",
	}
	writeJSON(w, http.StatusCreated, resp)
}

func (cp *CP) issue(hostID, csrPEM, caCSRPEM string) (*hostapi.EnrolResponse, error) {
	csr, err := pki.ParseCSR(csrPEM)
	if err != nil {
		return nil, err
	}
	der, err := cp.CA.SignClient(csr, hostID, cp.CertTTL)
	if err != nil {
		return nil, err
	}
	out := &hostapi.EnrolResponse{HostID: hostID, CA: cp.CA.PEM, TrustDomain: cp.TrustDomain}
	if caCSRPEM != "" {
		caCSR, err := pki.ParseCSR(caCSRPEM)
		if err != nil {
			return nil, err
		}
		caDer, err := cp.CA.SignSVIDCA(caCSR, cp.TrustDomain, hostID, cp.CertTTL)
		if err != nil {
			return nil, err
		}
		out.SVIDIntermediate = pki.CertPEM(caDer)
	}
	cert, _ := x509.ParseCertificate(der)
	cp.mu.Lock()
	h := cp.hosts[hostID]
	if h.cert != nil {
		delete(cp.byFP, pki.Fingerprint(h.cert)) // only the latest certificate is pinned
	}
	h.cert = cert
	cp.byFP[pki.Fingerprint(cert)] = hostID
	cp.mu.Unlock()
	out.Certificate = pki.CertPEM(der)
	out.CertificateNotAfter = cert.NotAfter.UTC().Format(time.RFC3339)
	out.RenewAfter = pki.RenewAt(cert).UTC().Format(time.RFC3339)
	return out, nil
}

// authHost enforces the BossHost proof: a certificate this CA issued and
// pinned to the host, a valid signature, htm/htu/iat, a single-use jti, the
// host not revoked, and the path's host id being the caller's own.
func (cp *CP) authHost(w http.ResponseWriter, r *http.Request, id string) (string, bool) {
	scheme, token, _ := strings.Cut(r.Header.Get("Authorization"), " ")
	if !strings.EqualFold(scheme, hostapi.AuthScheme) || token == "" {
		cp.refuse(w, http.StatusUnauthorized, "host_credential_required", "this route answers only an enrolled agent host")
		return "", false
	}
	fp, claims, err := pki.ParseHostProof(token)
	if err != nil {
		cp.refuse(w, http.StatusUnauthorized, err.Error(), "bad host proof")
		return "", false
	}
	cp.mu.Lock()
	hostID, known := cp.byFP[fp]
	var h *host
	revoked := false
	if known {
		h = cp.hosts[hostID]
		revoked = h.revoked // read under the lock: Revoke writes it
	}
	spent := cp.jtis[claims.Jti]
	cp.jtis[claims.Jti] = true
	cp.mu.Unlock()
	switch {
	case !known:
		cp.refuse(w, http.StatusUnauthorized, hostapi.ReasonCertUnknown, "unknown host certificate")
		return "", false
	case revoked:
		cp.refuse(w, http.StatusForbidden, hostapi.ReasonHostRevoked, "this host is revoked.")
		return "", false
	}
	if _, err := pki.VerifyHostProof(token, h.cert, r.Method, r.URL.Path, time.Now()); err != nil {
		cp.refuse(w, http.StatusUnauthorized, err.Error(), "host proof refused")
		return "", false
	}
	if spent {
		cp.refuse(w, http.StatusUnauthorized, hostapi.ReasonProofReplay, "proof replayed")
		return "", false
	}
	if id != "" && id != hostID {
		cp.refuse(w, http.StatusForbidden, "not_this_host", "a host acts only for itself")
		return "", false
	}
	return hostID, true
}

func (cp *CP) hostRoute(w http.ResponseWriter, r *http.Request) {
	rest := strings.Trim(strings.TrimPrefix(r.URL.Path, "/v1/hosts/"), "/")
	parts := strings.Split(rest, "/")
	if len(parts) != 2 {
		http.NotFound(w, r)
		return
	}
	id, verb := parts[0], parts[1]
	if verb == "events" && cp.NoEvents {
		http.NotFound(w, r)
		return
	}
	if _, ok := cp.authHost(w, r, id); !ok {
		return
	}
	switch {
	case verb == "heartbeat" && r.Method == http.MethodPost:
		var hb hostapi.HeartbeatRequest
		if err := json.NewDecoder(io.LimitReader(r.Body, 4<<20)).Decode(&hb); err != nil {
			http.Error(w, "bad body", http.StatusBadRequest)
			return
		}
		cp.mu.Lock()
		h := cp.hosts[id]
		h.heartbeats++
		h.last = &hb
		h.lastAt = time.Now()
		var gen int64
		if d, ok := cp.desired[id]; ok {
			gen = d.Generation
		}
		renew := time.Until(h.cert.NotAfter) <= h.cert.NotAfter.Sub(h.cert.NotBefore)/3
		cp.mu.Unlock()
		cp.observeReplicas(id, hb.Replicas, -1, false) // as fleet.heartbeat's _observe does
		writeJSON(w, http.StatusOK, hostapi.HeartbeatResponse{HostID: id, DesiredGeneration: gen,
			Status: "ready", ReplicasSeen: len(hb.Replicas), Renew: renew, HeartbeatSeconds: 2})
	case verb == "observed" && r.Method == http.MethodPost:
		var ob hostapi.ObservedRequest
		if err := json.NewDecoder(io.LimitReader(r.Body, 4<<20)).Decode(&ob); err != nil {
			http.Error(w, "bad body", http.StatusBadRequest)
			return
		}
		cp.mu.Lock()
		cp.hosts[id].observed = ob.Replicas
		cp.mu.Unlock()
		cp.observeReplicas(id, ob.Replicas, ob.StateGen, false)
		writeJSON(w, http.StatusOK, map[string]any{"host_id": id, "replicas_seen": len(ob.Replicas)})
	case verb == "replicas" && r.Method == http.MethodPut:
		cp.putReplicas(w, r, id)
	case verb == "renew" && r.Method == http.MethodPost:
		var req hostapi.RenewRequest
		if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&req); err != nil {
			http.Error(w, "bad body", http.StatusBadRequest)
			return
		}
		resp, err := cp.issue(id, req.CSR, req.SVIDCSR)
		if err != nil {
			cp.refuse(w, http.StatusBadRequest, "csr_invalid", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, resp)
	case verb == "desired" && r.Method == http.MethodGet:
		cp.serveDesired(w, r, id)
	case verb == "guest-calls" && r.Method == http.MethodPost:
		var b hostapi.GuestCallBatch
		if err := json.NewDecoder(io.LimitReader(r.Body, 8<<20)).Decode(&b); err != nil {
			http.Error(w, "bad body", http.StatusBadRequest)
			return
		}
		accepted, dup, rejected := cp.ingest(b.Calls)
		writeJSON(w, http.StatusOK, map[string]any{"accepted": accepted, "duplicates": dup, "rejected": rejected})
	case verb == "events" && r.Method == http.MethodPost:
		var b hostapi.HostEventBatch
		if err := json.NewDecoder(io.LimitReader(r.Body, 8<<20)).Decode(&b); err != nil {
			http.Error(w, "bad body", http.StatusBadRequest)
			return
		}
		cp.mu.Lock()
		cp.events = append(cp.events, b.Events...)
		cp.mu.Unlock()
		writeJSON(w, http.StatusOK, map[string]any{"accepted": len(b.Events)})
	default:
		http.NotFound(w, r)
	}
}

// handleShaped mirrors the control plane's refusal of raw handles in audit rows.
var handleShaped = regexp.MustCompile(`boss_(mdl|egr|mcpgw|run|sak|pat|hjt)_[A-Za-z0-9_\-]{6,}`)

var guestRoutes = map[string]bool{"model": true, "git": true, "api": true, "mcp": true, "run_plane": true, "connect": true}

// ingest validates rows exactly as fleet.ingest_guest_calls does.
func (cp *CP) ingest(calls []hostapi.GuestCall) (int, int, []map[string]any) {
	cp.mu.Lock()
	defer cp.mu.Unlock()
	accepted, dup := 0, 0
	var rejected []map[string]any
	for i, c := range calls {
		raw, _ := json.Marshal(c)
		reason := ""
		switch {
		case c.CallID == "":
			reason = "call_id_missing"
		case !guestRoutes[c.Route]:
			reason = "route_unknown"
		case handleShaped.Match(raw):
			reason = "raw_handle_refused"
		case c.Verdict != "" && c.Verdict != "allow" && c.Verdict != "deny":
			reason = "verdict_unknown"
		}
		if reason != "" {
			rejected = append(rejected, map[string]any{"index": i, "reason": reason})
			continue
		}
		if cp.callIDs[c.CallID] {
			dup++
			continue
		}
		cp.callIDs[c.CallID] = true
		cp.calls = append(cp.calls, c)
		accepted++
	}
	return accepted, dup, rejected
}

func (cp *CP) serveDesired(w http.ResponseWriter, r *http.Request, id string) {
	since, _ := strconv.ParseInt(r.URL.Query().Get("since"), 10, 64)
	wait, _ := strconv.Atoi(r.URL.Query().Get("wait"))
	if wait < 0 || wait > 30 {
		wait = 30
	}
	deadline := time.NewTimer(time.Duration(wait) * time.Second)
	defer deadline.Stop()
	for {
		cp.mu.Lock()
		d, ok := cp.desired[id]
		var gen int64
		if ok {
			gen = d.Generation
		}
		ch := cp.changed
		if gen > since || since == 0 {
			out := hostapi.DesiredState{HostID: id, Endpoints: map[string]string{"run_plane": "/v1/run-plane"}}
			if ok {
				out = *d
			}
			out.Changed = true
			cp.mu.Unlock()
			writeJSON(w, http.StatusOK, out)
			return
		}
		cp.mu.Unlock()
		select {
		case <-ch:
		case <-deadline.C:
			writeJSON(w, http.StatusOK, map[string]any{"changed": false, "host_id": id, "generation": gen})
			return
		case <-r.Context().Done():
			return
		}
	}
}

// ---- Admin (boss-fakecp binary; loopback callers only) -------------------

func loopback(r *http.Request) bool {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return false
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func (cp *CP) adminDesired(w http.ResponseWriter, r *http.Request) {
	if !loopback(r) {
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}
	id := strings.Trim(strings.TrimPrefix(r.URL.Path, "/admin/desired/"), "/")
	switch r.Method {
	case http.MethodPut:
		var ds hostapi.DesiredState
		if err := json.NewDecoder(io.LimitReader(r.Body, 4<<20)).Decode(&ds); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		gen := cp.SetDesired(id, ds)
		writeJSON(w, http.StatusOK, map[string]any{"generation": gen})
	case http.MethodGet:
		writeJSON(w, http.StatusOK, cp.Desired(id))
	default:
		http.Error(w, "method", http.StatusMethodNotAllowed)
	}
}

func (cp *CP) adminRevoke(w http.ResponseWriter, r *http.Request) {
	if !loopback(r) || r.Method != http.MethodPost {
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}
	cp.Revoke(strings.Trim(strings.TrimPrefix(r.URL.Path, "/admin/revoke/"), "/"))
	writeJSON(w, http.StatusOK, map[string]any{"revoked": true})
}

func (cp *CP) adminState(w http.ResponseWriter, r *http.Request) {
	if !loopback(r) {
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}
	out := map[string]any{}
	for _, id := range cp.HostIDs() {
		hb, at := cp.LastHeartbeat(id)
		out[id] = map[string]any{"heartbeats": cp.HeartbeatCount(id), "last": hb, "last_at": at}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"hosts": out, "events": cp.Events(), "guest_calls": cp.GuestCalls(),
		"refusals": map[string]int{hostapi.ReasonHostRevoked: cp.Refusals(hostapi.ReasonHostRevoked)},
	})
}

// WaitFor polls cond until it is true or ctx ends.
func WaitFor(ctx context.Context, every time.Duration, cond func() bool) bool {
	for {
		if cond() {
			return true
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(every):
		}
	}
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
