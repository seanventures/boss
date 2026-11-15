package gateway

import (
	"bufio"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/guest"
	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanheiney/boss/services/sandbox-service/internal/pki"
)

type rig struct {
	t      *testing.T
	gw     *Gateway
	dir    string
	ca     *pki.CA
	mu     sync.Mutex
	calls  []hostapi.GuestCall
	events []hostapi.HostEvent
	seen   []http.Header
	peers  []string
	up     *httptest.Server
}

func newRig(t *testing.T) *rig {
	t.Helper()
	ca, err := pki.NewCA("root")
	if err != nil {
		t.Fatal(err)
	}
	r := &rig{t: t, ca: ca}
	// Host intermediate, as enrolment would deliver it.
	sk, _ := pki.GenerateKey()
	csrPEM, _ := pki.CSR(sk, "svid-ca")
	csr, _ := pki.ParseCSR(csrPEM)
	der, err := ca.SignSVIDCA(csr, "inst.boss", "hst_1", time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	cert, _ := x509.ParseCertificate(der)
	creds := &pki.HostCreds{HostID: "hst_1", TrustDomain: "inst.boss", SVIDCAKey: sk, SVIDCACert: cert}
	pool := x509.NewCertPool()
	pool.AddCert(ca.Cert)

	r.up = httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		r.mu.Lock()
		r.seen = append(r.seen, req.Header.Clone())
		if len(req.TLS.PeerCertificates) > 0 && len(req.TLS.PeerCertificates[0].URIs) > 0 {
			r.peers = append(r.peers, req.TLS.PeerCertificates[0].URIs[0].String())
		} else {
			r.peers = append(r.peers, "")
		}
		r.mu.Unlock()
		if req.URL.Path == "/v1/run-plane/admit" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"run_id":"run_adm","status":"leased","lease":{"replica_id":"rep_b","job_id":"job_adm","fence_epoch":5,"run_token":"lease_SECRET_adm"}}`))
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		for i := 0; i < 3; i++ {
			_, _ = w.Write([]byte("data: chunk" + strconv.Itoa(i) + "\n\n"))
			w.(http.Flusher).Flush()
		}
	}))
	sc, _ := ca.ServerCert("127.0.0.1")
	// Verify-if-given: SVID routes present a VM certificate; the run-plane
	// route authenticates the host by its proof instead.
	r.up.TLS = &tls.Config{Certificates: []tls.Certificate{sc}, ClientCAs: pool, ClientAuth: tls.VerifyClientCertIfGiven}
	r.up.StartTLS()
	t.Cleanup(r.up.Close)

	r.gw = New("hst_1", pki.NewMinter(func() *pki.HostCreds { return creds }), pool)
	r.gw.HostProof = func(method, path string) (string, error) { return "proof-for-" + method + "-" + path, nil }
	r.gw.Audit = func(c hostapi.GuestCall) { r.mu.Lock(); r.calls = append(r.calls, c); r.mu.Unlock() }
	r.gw.Event = func(e hostapi.HostEvent) { r.mu.Lock(); r.events = append(r.events, e); r.mu.Unlock() }
	dir, err := os.MkdirTemp("", "gw")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	r.dir = dir
	r.gw.Update(hostapi.DesiredState{
		Endpoints: map[string]string{"model_gateway": r.up.URL, "run_plane": r.up.URL + "/v1/run-plane", "egress": r.up.URL, "mcp_gateway": r.up.URL},
		VMs: []hostapi.DesiredVM{
			{ReplicaID: "rep_a", VMID: "vm-a", DeploymentID: "dep", Mode: hostapi.ModePerRun,
				Runs: []hostapi.RunBind{{RunID: "run_7", Epoch: 3, Handles: hostapi.Handles{Model: "boss_mdl_A", Run: "boss_run_A", Egress: "boss_egr_A"}}}},
			{ReplicaID: "rep_b", VMID: "vm-b", DeploymentID: "dep", Mode: hostapi.ModeReplica, ReplicaHandle: "boss_rep_B"},
		},
	})
	for _, id := range []string{"vm-a", "vm-b"} {
		id := id
		if err := r.gw.Attach(id, VMInfo{DeploymentID: "dep"}, func(port int) (net.Listener, error) {
			p := filepath.Join(dir, id+"_"+strconv.Itoa(port))
			return net.Listen("unix", p)
		}); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() { r.gw.Detach("vm-a"); r.gw.Detach("vm-b") })
	return r
}

// call sends one HTTP request on a VM's guest socket, as boss-vsockd would.
func (r *rig) call(vm string, port int, method, path string, hdr map[string]string) (int, string) {
	r.t.Helper()
	c := &http.Client{Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return net.Dial("unix", filepath.Join(r.dir, vm+"_"+strconv.Itoa(port)))
	}}, Timeout: 5 * time.Second}
	req, _ := http.NewRequest(method, "http://127.0.0.1"+path, strings.NewReader(`{"x":1}`))
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	resp, err := c.Do(req)
	if err != nil {
		r.t.Fatal(err)
	}
	defer resp.Body.Close()
	var b strings.Builder
	sc := bufio.NewScanner(resp.Body)
	for sc.Scan() {
		b.WriteString(sc.Text() + "\n")
	}
	return resp.StatusCode, b.String()
}

func (r *rig) lastCall() hostapi.GuestCall {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.calls[len(r.calls)-1]
}

// The gateway skeleton's core promise: a guest-supplied Authorization header
// (or any other credential / identity header) never survives.
func TestGuestAuthorizationNeverSurvives(t *testing.T) {
	r := newRig(t)
	status, body := r.call("vm-a", guest.PortModel, "POST", "/anthropic/v1/messages", map[string]string{
		"Authorization":       "Bearer boss_mdl_A",
		"X-Api-Key":           "sk-ant-guest-supplied",
		"Proxy-Authorization": "Basic Zm9vOmJhcg==",
		"Cookie":              "s=1",
		"X-Boss-Run-Id":       "run_forged",
		"Boss-Run-Assertion":  "forged.assertion.sig",
		"X-Forwarded-For":     "1.2.3.4",
		"Anthropic-Version":   "2023-06-01",
	})
	if status != 200 || !strings.Contains(body, "chunk2") {
		t.Fatalf("status %d body %q", status, body)
	}
	h := r.seen[len(r.seen)-1]
	for _, name := range []string{"X-Api-Key", "Proxy-Authorization", "Cookie", "X-Boss-Run-Id", "X-Forwarded-For"} {
		if v := h.Get(name); v != "" {
			t.Fatalf("guest header %s survived to the upstream", name)
		}
	}
	// The guest's Authorization is gone; what the upstream sees there is the HOST's proof
	// (P8b S10, HOST_API.md §4.6), and the guest's handle only in X-Boss-Handle.
	if a := h.Get("Authorization"); a != "BossHost proof-for-POST-/anthropic/v1/messages" || strings.Contains(a, "boss_mdl_A") {
		t.Fatalf("Authorization at the upstream = %q", a)
	}
	if a := h.Get(AssertionHeader); a == "" || a == "forged.assertion.sig" {
		t.Fatal("the upstream must see the host's run assertion, never the guest's")
	}
	if h.Get("Anthropic-Version") != "2023-06-01" {
		t.Fatal("ordinary headers must pass through")
	}
	if got := r.peers[len(r.peers)-1]; got != "spiffe://inst.boss/host/hst_1/vm/vm-a" {
		t.Fatalf("upstream mTLS peer = %s", got)
	}
	// The assertion names the run and epoch the handle is bound to.
	parts := strings.Split(h.Get(AssertionHeader), ".")
	raw, _ := b64.DecodeString(parts[1])
	var a RunAssertion
	_ = json.Unmarshal(raw, &a)
	if a.Run != "run_7" || a.Epoch != 3 || a.VM != "vm-a" || a.Aud != RouteModel {
		t.Fatalf("assertion claims: %+v", a)
	}
	c := r.lastCall()
	if c.Verdict != "allow" || c.RunID != "run_7" || c.Epoch == nil || *c.Epoch != 3 || c.HandleFP != Fingerprint("boss_mdl_A") || c.BytesIn == 0 || c.CallID == "" || c.Route != "model" {
		t.Fatalf("audit row: %+v", c)
	}
	raw, _ = json.Marshal(c)
	if strings.Contains(string(raw), "boss_mdl_A") {
		t.Fatal("an audit row carried the raw handle")
	}
}

func TestStripGuestHeadersIsCaseInsensitive(t *testing.T) {
	h := http.Header{}
	h["authorization"] = []string{"Bearer x"}
	h["X-API-KEY"] = []string{"k"}
	h.Set("x-boss-anything", "y")
	h.Set("Content-Type", "application/json")
	StripGuestHeaders(h)
	if len(h) != 1 || h.Get("Content-Type") == "" {
		t.Fatalf("left: %v", h)
	}
}

func TestHandleReplayFromAnotherVMIsRefusedAndRevoked(t *testing.T) {
	r := newRig(t)
	status, _ := r.call("vm-b", guest.PortModel, "POST", "/anthropic/v1/messages", map[string]string{"Authorization": "Bearer boss_mdl_A"})
	if status != http.StatusForbidden || r.lastCall().RejectedBy != "handle_replay" {
		t.Fatalf("replay: %d %+v", status, r.lastCall())
	}
	if len(r.events) != 1 || r.events[0].Kind != "handle_replay" {
		t.Fatalf("handle_replay event: %+v", r.events)
	}
	// The handle is now revoked even on its own VM.
	status, _ = r.call("vm-a", guest.PortModel, "POST", "/anthropic/v1/messages", map[string]string{"Authorization": "Bearer boss_mdl_A"})
	if status != http.StatusForbidden || r.lastCall().RejectedBy != "handle_revoked" {
		t.Fatalf("after replay: %d %+v", status, r.lastCall())
	}
}

func TestFencedEpochGets409(t *testing.T) {
	r := newRig(t)
	ds := hostapi.DesiredState{
		Endpoints: map[string]string{"run_plane": r.up.URL},
		VMs: []hostapi.DesiredVM{{VMID: "vm-a", Mode: hostapi.ModePerRun,
			Runs: []hostapi.RunBind{{RunID: "run_7", Epoch: 3, Handles: hostapi.Handles{Run: "boss_run_A"}}}}},
		Fences: []hostapi.Fence{{RunID: "run_7", MinEpoch: 4}},
	}
	r.gw.Update(ds)
	status, _ := r.call("vm-a", guest.PortRunPlane, "POST", "/v1/run-plane/checkpoints", map[string]string{"Authorization": "Bearer boss_run_A"})
	if status != http.StatusConflict || r.lastCall().RejectedBy != "fenced" {
		t.Fatalf("fenced: %d %+v", status, r.lastCall())
	}
}

func TestRouteTableDenies(t *testing.T) {
	r := newRig(t)
	cases := []struct {
		port         int
		method, path string
		hdr          map[string]string
		want         int
		reason       string
	}{
		{guest.PortModel, "GET", "/anthropic/v1/models", map[string]string{"Authorization": "Bearer boss_mdl_A"}, 403, "route_denied"},
		{guest.PortModel, "POST", "/anthropic/v1/messages", nil, 401, "handle_required"},
		{guest.PortModel, "POST", "/anthropic/v1/messages", map[string]string{"Authorization": "Bearer boss_mdl_unknown"}, 403, "handle_unknown"},
		{guest.PortGit, "GET", "/git/github.com/o/r/info/refs", map[string]string{"Authorization": "Bearer boss_mdl_A"}, 403, "handle_wrong_route"},
		{guest.PortConnect, "GET", "/", nil, 403, "route_denied"},
		{guest.PortMCP, "POST", "/not-mcp", nil, 403, "route_denied"},
		{guest.PortRunPlane, "POST", "/v1/other", nil, 403, "route_denied"},
	}
	for _, c := range cases {
		got, _ := r.call("vm-a", c.port, c.method, c.path, c.hdr)
		if got != c.want || r.lastCall().RejectedBy != c.reason {
			t.Errorf("%d %s %s: got %d %q, want %d %q", c.port, c.method, c.path, got, r.lastCall().RejectedBy, c.want, c.reason)
		}
	}
	// A replica handle reaches the run plane.
	if got, _ := r.call("vm-b", guest.PortRunPlane, "POST", "/v1/run-plane/admit", map[string]string{"Authorization": "Bearer boss_rep_B"}); got != 200 {
		t.Fatalf("replica handle on the run plane: %d", got)
	}
}

// The run plane (P8a): a replica needs no handle — its socket is its
// identity. The forward carries the HOST's proof, X-Boss-Replica set from the
// socket (a forged one is replaced), and the lease headers; nothing else the
// guest claimed. A per_run VM without a handle is refused.
func TestRunPlaneSocketIdentityAndLeasePassthrough(t *testing.T) {
	r := newRig(t)
	r.gw.Runs.Put("run_9", RunLease{ReplicaID: "rep_b", JobID: "job_1", Token: "lease_host_tok", Epoch: 4})
	status, _ := r.call("vm-b", guest.PortRunPlane, "PUT", "/v1/run-plane/runs/run_9/checkpoints", map[string]string{
		"Authorization": "Bearer not-a-handle", "X-Boss-Replica": "rep_forged", "X-Boss-Job": "job_1",
		"X-Boss-Run-Token": "guest-forged-token", "X-Boss-Fence-Epoch": "4", "X-Boss-Org": "org_forged",
	})
	if status != 200 {
		t.Fatalf("socket-identified replica: %d %+v", status, r.lastCall())
	}
	h := r.seen[len(r.seen)-1]
	if h.Get("Authorization") != "BossHost proof-for-PUT-/v1/run-plane/runs/run_9/checkpoints" {
		t.Fatalf("the forward must carry the host proof for the upstream path, got %q", h.Get("Authorization"))
	}
	if h.Get("X-Boss-Replica") != "rep_b" || h.Get("X-Boss-Org") != "" {
		t.Fatalf("identity headers: replica=%q org=%q", h.Get("X-Boss-Replica"), h.Get("X-Boss-Org"))
	}
	if h.Get("X-Boss-Run-Token") != "lease_host_tok" {
		t.Fatal("the run token must be the host's, never the guest's")
	}
	if h.Get("X-Boss-Job") != "job_1" || h.Get("X-Boss-Fence-Epoch") != "4" {
		t.Fatalf("lease headers must pass through: %v", h)
	}
	if r.lastCall().Route != "run_plane" || r.lastCall().Verdict != "allow" || r.lastCall().RunID != "run_9" {
		t.Fatalf("audit: %+v", r.lastCall())
	}
	if got, _ := r.call("vm-a", guest.PortRunPlane, "POST", "/v1/run-plane/admit", nil); got != 401 {
		t.Fatalf("per_run with no handle must be refused, got %d", got)
	}
}

// HOST_API.md §4.5: a run the host holds no token for (or another replica's
// run) is 409 fenced without a forward; settle forgets the token; an admit
// response loses lease.run_token on the way to the guest.
func TestRunTokensStayOnTheHost(t *testing.T) {
	r := newRig(t)
	n := len(r.seen)
	got, _ := r.call("vm-b", guest.PortRunPlane, "PUT", "/v1/run-plane/runs/run_unknown/checkpoints",
		map[string]string{"X-Boss-Run-Token": "guest-guess"})
	if got != 409 || r.lastCall().RejectedBy != "fenced" || len(r.seen) != n {
		t.Fatalf("unknown run: %d %+v (forwarded %d)", got, r.lastCall(), len(r.seen)-n)
	}
	r.gw.Runs.Put("run_other", RunLease{ReplicaID: "rep_x", Token: "t"})
	if got, _ := r.call("vm-b", guest.PortRunPlane, "GET", "/v1/run-plane/runs/run_other", nil); got != 409 {
		t.Fatalf("another replica's run must be fenced: %d", got)
	}
	r.gw.Runs.Put("run_s", RunLease{ReplicaID: "rep_b", Token: "lease_s", Epoch: 2})
	if got, _ := r.call("vm-b", guest.PortRunPlane, "POST", "/v1/run-plane/runs/run_s/lease",
		map[string]string{"Content-Type": "application/json"}); got != 200 {
		t.Fatalf("lease call: %d", got)
	}
	if _, ok := r.gw.Runs.Get("run_s"); !ok {
		t.Fatal("a non-ending lease call must keep the token")
	}
	c := &http.Client{Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return net.Dial("unix", filepath.Join(r.dir, "vm-b_"+strconv.Itoa(guest.PortRunPlane)))
	}}}
	resp, err := c.Post("http://127.0.0.1/v1/run-plane/runs/run_s/lease", "application/json", strings.NewReader(`{"action":"settle"}`))
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("settle: %v", err)
	}
	resp.Body.Close()
	if _, ok := r.gw.Runs.Get("run_s"); ok {
		t.Fatal("settle must forget the run token")
	}
	resp, err = c.Post("http://127.0.0.1/v1/run-plane/admit", "application/json", strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if strings.Contains(string(body), "lease_SECRET_adm") || !strings.Contains(string(body), "job_adm") {
		t.Fatalf("admit body to the guest: %s", body)
	}
	if l, ok := r.gw.Runs.Get("run_adm"); !ok || l.Token != "lease_SECRET_adm" || l.ReplicaID != "rep_b" || l.Epoch != 5 {
		t.Fatalf("admit lease not kept by the host: %+v %v", l, ok)
	}
}

func TestRelativeEndpointsResolveAgainstTheControlPlane(t *testing.T) {
	g := New("h", nil, nil)
	g.CPBase = mustURL("https://cp.example:8443")
	u, err := g.resolveEndpoint("/v1/run-plane")
	if err != nil || u.String() != "https://cp.example:8443/v1/run-plane" {
		t.Fatalf("%v %v", u, err)
	}
	if joinPath(u.Path, "/v1/run-plane/next") != "/v1/run-plane/next" || joinPath("/mdl", "/anthropic/v1/messages") != "/mdl/anthropic/v1/messages" {
		t.Fatal("joinPath")
	}
	if _, err := g.resolveEndpoint("http://10.0.0.1/x"); err == nil {
		t.Fatal("plain http off-loopback must be refused")
	}
}

func TestFailsClosedWithoutEndpoint(t *testing.T) {
	r := newRig(t)
	r.gw.Update(hostapi.DesiredState{VMs: []hostapi.DesiredVM{{VMID: "vm-a", Mode: hostapi.ModePerRun,
		Runs: []hostapi.RunBind{{RunID: "run_7", Epoch: 3, Handles: hostapi.Handles{Model: "boss_mdl_A"}}}}}})
	if got, _ := r.call("vm-a", guest.PortModel, "POST", "/anthropic/v1/messages", map[string]string{"Authorization": "Bearer boss_mdl_A"}); got != 503 {
		t.Fatalf("no endpoint must be a visible 503, got %d", got)
	}
	if !upstreamAllowed(mustURL("http://127.0.0.1:1")) || upstreamAllowed(mustURL("http://10.0.0.1")) {
		t.Fatal("plain http is allowed to loopback only")
	}
}

func TestDisabledGatewayRefuses(t *testing.T) {
	r := newRig(t)
	r.gw.Enabled = func() bool { return false }
	if got, _ := r.call("vm-a", guest.PortModel, "POST", "/anthropic/v1/messages", map[string]string{"Authorization": "Bearer boss_mdl_A"}); got != 503 {
		t.Fatalf("revoked host must refuse: %d", got)
	}
}

func TestBatcherKeepsRowsUntilFlushSucceeds(t *testing.T) {
	var mu sync.Mutex
	var got []int
	fail := true
	b := &Batcher[int]{MaxBatch: 2, Cap: 5, Flush: func(_ context.Context, rows []int) error {
		mu.Lock()
		defer mu.Unlock()
		if fail {
			return errors.New("down")
		}
		got = append(got, rows...)
		return nil
	}}
	for i := 0; i < 7; i++ {
		b.Add(i)
	}
	b.flushOnce(context.Background())
	if b.Dropped() != 2 {
		t.Fatalf("dropped %d, want 2 (cap 5)", b.Dropped())
	}
	fail = false
	b.flushOnce(context.Background())
	if len(got) != 5 || got[0] != 2 || got[4] != 6 {
		t.Fatalf("flushed %v", got)
	}
}

func TestPathTemplateDropsIdentifiers(t *testing.T) {
	if got := pathTemplate("/v1/run-plane/run_123/checkpoints/abc"); got != "/v1/run-plane/run_123/*" {
		t.Fatal(got)
	}
}

// VMs never poll for work (§3.13.0): next and hosts/next are blocked at the
// gateway even for a replica the socket identifies.
func TestVMsCannotPollForWork(t *testing.T) {
	r := newRig(t)
	for _, p := range []string{"/v1/run-plane/next", "/v1/run-plane/hosts/next", "/v1/run-plane/hosts/x"} {
		got, _ := r.call("vm-b", guest.PortRunPlane, "POST", p, map[string]string{"Authorization": "Bearer boss_rep_B"})
		if got != 403 || r.lastCall().RejectedBy != "route_denied" {
			t.Fatalf("%s: %d %+v", p, got, r.lastCall())
		}
	}
	if len(r.seen) != 0 {
		t.Fatal("a work poll reached the control plane")
	}
}

func TestPathHygieneAndPrefixBoundary(t *testing.T) {
	r := newRig(t)
	auth := map[string]string{"Authorization": "Bearer boss_mdl_A"}
	if got, _ := r.call("vm-a", guest.PortModel, "POST", "/anthropic/v1/messagesX", auth); got != 403 {
		t.Fatalf("prefix must stop at a segment boundary: %d", got)
	}
	for _, p := range []string{"/anthropic/v1/messages/../../admin", "/anthropic//v1/messages",
		"/anthropic/v1/messages%2f..%2fx", "/anthropic/v1/%2e%2e/messages"} {
		got, _ := r.call("vm-a", guest.PortModel, "POST", p, auth)
		if got == 200 {
			t.Fatalf("%s was forwarded", p)
		}
	}
	if !CleanPath("/v1/run-plane/runs/r1/checkpoints", "/v1/run-plane/runs/r1/checkpoints") || CleanPath("/a/./b", "/a/./b") {
		t.Fatal("CleanPath")
	}
}

func TestMoreCredentialHeadersAreStripped(t *testing.T) {
	h := http.Header{}
	for _, k := range []string{"X-Auth-Token", "Private-Token", "X-Amz-Security-Token"} {
		h.Set(k, "secret-ish")
	}
	StripGuestHeaders(h)
	if len(h) != 0 {
		t.Fatalf("left: %v", h)
	}
}

func TestGuestTrailersDoNotSurvive(t *testing.T) {
	r := newRig(t)
	c := &http.Client{Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return net.Dial("unix", filepath.Join(r.dir, "vm-a_"+strconv.Itoa(guest.PortModel)))
	}}}
	body := strings.NewReader(`{"x":1}`)
	req, _ := http.NewRequest("POST", "http://127.0.0.1/anthropic/v1/messages", io.NopCloser(body))
	req.ContentLength = -1 // chunked, so trailers are sent
	req.Header.Set("Authorization", "Bearer boss_mdl_A")
	req.Trailer = http.Header{"Authorization": {"Bearer smuggled"}, "X-Boss-Replica": {"forged"}}
	resp, err := c.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	h := r.seen[len(r.seen)-1]
	if strings.Contains(h.Get("Authorization"), "smuggled") || h.Get("X-Boss-Replica") != "" {
		t.Fatalf("a trailer was smuggled through: %v", h)
	}
}

// The front-trim race: rows dropped from the front while a flush is in flight
// must not make the flush remove rows it never sent.
func TestBatcherNeverDropsUnsentRowsAfterFrontTrim(t *testing.T) {
	var got []int
	var b *Batcher[int]
	first := true
	b = &Batcher[int]{MaxBatch: 3, Cap: 4, Flush: func(_ context.Context, rows []int) error {
		if first {
			first = false
			for i := 10; i < 13; i++ { // three new rows arrive mid-flush; the cap trims the front
				b.Add(i)
			}
		}
		got = append(got, rows...)
		return nil
	}}
	for i := 0; i < 4; i++ {
		b.Add(i)
	}
	b.flushOnce(context.Background())
	// Sent 0,1,2 first; meanwhile 0,1,2 were trimmed and 10,11,12 added: buf = 3,10,11,12.
	// None of those may be lost.
	want := []int{0, 1, 2, 3, 10, 11, 12}
	if len(got) != len(want) {
		t.Fatalf("flushed %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("flushed %v, want %v", got, want)
		}
	}
}

// HOST_API.md §4.6 (P8b S10): a model forward carries the HOST's proof over the
// gateway path, the guest's handle moved into X-Boss-Handle, and the host's word
// on the VM, run and epoch — while still presenting the VM's SVID. The run plane
// keeps its own shape (host transport, X-Boss-Replica, no handle header).
func TestModelForwardCarriesTheHostProofAndTheBoundHandle(t *testing.T) {
	r := newRig(t)
	status, _ := r.call("vm-a", guest.PortModel, "POST", "/anthropic/v1/messages", map[string]string{
		"X-Api-Key": "boss_mdl_A", "X-Boss-Handle": "boss_mdl_FORGED", "X-Boss-Vm": "vm-forged",
		"X-Boss-Run": "run_forged", "X-Boss-Fence-Epoch": "99",
	})
	if status != 200 {
		t.Fatalf("status %d %+v", status, r.lastCall())
	}
	h := r.seen[len(r.seen)-1]
	if h.Get("Authorization") != "BossHost proof-for-POST-/anthropic/v1/messages" {
		t.Fatalf("the forward must carry the host proof for the gateway path, got %q", h.Get("Authorization"))
	}
	if h.Get("X-Boss-Handle") != "boss_mdl_A" || h.Get("X-Api-Key") != "" {
		t.Fatalf("the guest's handle moves into X-Boss-Handle, nothing else: handle=%q key=%q", h.Get("X-Boss-Handle"), h.Get("X-Api-Key"))
	}
	if h.Get("X-Boss-Vm") != "vm-a" || h.Get("X-Boss-Run") != "run_7" || h.Get("X-Boss-Fence-Epoch") != "3" || h.Get("X-Boss-Deployment") != "dep" {
		t.Fatalf("the binding headers are the HOST's word, never the guest's: %v", h)
	}
	if got := r.peers[len(r.peers)-1]; got != "spiffe://inst.boss/host/hst_1/vm/vm-a" {
		t.Fatalf("a model forward still presents the VM's SVID, got %q", got)
	}
	for _, rule := range DefaultRules() {
		if rule.Route == RouteModel && !rule.Deny && !rule.HostProof {
			t.Fatalf("model rule %s must carry the host proof", rule.ID)
		}
	}
}
