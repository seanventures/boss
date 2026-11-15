package gateway

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/guest"
	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
)

// P8b S12/S13 (BOSSAPP-1073, 1074): the MCP route takes the model gateway's
// contract; handles that arrive with a claimed run are bound to (run, epoch,
// the VM that took it); the host holds per-run DPoP keys and attaches one
// audience's token per destination; rotation never breaks a forward in
// flight.

// workItem is what hosts/next hands the host for run_9 on vm-a (HOST_API.md §2.8).
func bindWorkItem(r *rig) {
	r.gw.BindRun("vm-a", "rep_a", "dep", "run_9", 4, map[string]any{
		"mdl": "boss_mdl_W", "mcpgw": "boss_mcpgw_W", "run": "boss_run_W",
		"mcp_servers": map[string]any{"jira": "http://127.0.0.1:7103/mcp/jira"},
	})
}

func TestMCPForwardCarriesTheHostProofAndTheMovedHandle(t *testing.T) {
	r := newRig(t)
	bindWorkItem(r)
	status, _ := r.call("vm-a", guest.PortMCP, "POST", "/mcp/jira", map[string]string{
		"Authorization": "Bearer boss_mcpgw_W", "X-Boss-Handle": "boss_mcpgw_FORGED",
		"X-Boss-Vm": "vm-forged", "X-Boss-Run": "run_forged", "Cookie": "s=1", "DPoP": "guest.made.proof",
	})
	if status != 200 {
		t.Fatalf("status %d %+v", status, r.lastCall())
	}
	h := r.seen[len(r.seen)-1]
	if h.Get("Authorization") != "BossHost proof-for-POST-/mcp/jira" {
		t.Fatalf("the MCP forward carries the host proof for the gateway path, got %q", h.Get("Authorization"))
	}
	if h.Get("X-Boss-Handle") != "boss_mcpgw_W" {
		t.Fatalf("the guest's handle moves into X-Boss-Handle, got %q", h.Get("X-Boss-Handle"))
	}
	if h.Get("X-Boss-Vm") != "vm-a" || h.Get("X-Boss-Run") != "run_9" || h.Get("X-Boss-Fence-Epoch") != "4" || h.Get("X-Boss-Deployment") != "dep" {
		t.Fatalf("binding headers are the host's word: %v", h)
	}
	if h.Get("Cookie") != "" || h.Get("DPoP") != "" {
		t.Fatal("a guest cookie or a guest-made DPoP proof must never survive")
	}
	c := r.lastCall()
	if c.Route != RouteMCP || c.RunID != "run_9" || c.Epoch == nil || *c.Epoch != 4 || c.HandleFP != Fingerprint("boss_mcpgw_W") {
		t.Fatalf("audit row: %+v", c)
	}
	for _, rule := range DefaultRules() {
		if rule.Route == RouteMCP && !rule.Deny && (!rule.HostProof || rule.Audience != hostapi.AudMCP) {
			t.Fatalf("mcp rule %s must carry the host proof and the boss:mcp audience", rule.ID)
		}
	}
}

func TestMCPHandleFromVMAIsRefusedOnVMBAndRevoked(t *testing.T) {
	r := newRig(t)
	bindWorkItem(r)
	status, _ := r.call("vm-b", guest.PortMCP, "POST", "/mcp/jira", map[string]string{"Authorization": "Bearer boss_mcpgw_W"})
	if status != http.StatusForbidden || r.lastCall().RejectedBy != "handle_replay" {
		t.Fatalf("replay: %d %+v", status, r.lastCall())
	}
	if len(r.events) != 1 || r.events[0].Kind != "handle_replay" || r.events[0].VMID != "vm-b" {
		t.Fatalf("handle_replay event: %+v", r.events)
	}
	status, _ = r.call("vm-a", guest.PortMCP, "POST", "/mcp/jira", map[string]string{"Authorization": "Bearer boss_mcpgw_W"})
	if status != http.StatusForbidden || r.lastCall().RejectedBy != "handle_revoked" {
		t.Fatalf("after replay: %d %+v", status, r.lastCall())
	}
}

func TestRunBindingsOutliveDesiredStateUpdatesAndEndWithTheLease(t *testing.T) {
	r := newRig(t)
	bindWorkItem(r)
	// A desired-state resync rebuilds the desired bindings; the run's stay.
	r.gw.Update(hostapi.DesiredState{Endpoints: map[string]string{"mcp_gateway": r.up.URL, "run_plane": r.up.URL + "/v1/run-plane"},
		VMs: []hostapi.DesiredVM{{ReplicaID: "rep_a", VMID: "vm-a", DeploymentID: "dep", Mode: hostapi.ModePerRun}}})
	if status, _ := r.call("vm-a", guest.PortMCP, "POST", "/mcp/jira", map[string]string{"Authorization": "Bearer boss_mcpgw_W"}); status != 200 {
		t.Fatalf("run binding lost on Update: %d %+v", status, r.lastCall())
	}
	if r.gw.RunBound("run_9") != 3 {
		t.Fatalf("mcp_servers is not a handle; bound=%d", r.gw.RunBound("run_9"))
	}
	// A fence the control plane sends applies to run bindings too.
	r.gw.Update(hostapi.DesiredState{Endpoints: map[string]string{"mcp_gateway": r.up.URL},
		Fences: []hostapi.Fence{{RunID: "run_9", MinEpoch: 5}}})
	if status, _ := r.call("vm-a", guest.PortMCP, "POST", "/mcp/jira", map[string]string{"Authorization": "Bearer boss_mcpgw_W"}); status != http.StatusConflict || r.lastCall().RejectedBy != "fenced" {
		t.Fatalf("stale epoch: %d %+v", status, r.lastCall())
	}
	r.gw.UnbindRun("run_9")
	if status, _ := r.call("vm-a", guest.PortMCP, "POST", "/mcp/jira", map[string]string{"Authorization": "Bearer boss_mcpgw_W"}); status != http.StatusForbidden || r.lastCall().RejectedBy != "handle_unknown" {
		t.Fatalf("after the lease ended: %d %+v", status, r.lastCall())
	}
}

func TestMCPOnlyAcceptsPOST(t *testing.T) {
	r := newRig(t)
	bindWorkItem(r)
	before := len(r.seen)
	for _, m := range []string{"GET", "DELETE"} {
		status, _ := r.call("vm-a", guest.PortMCP, m, "/mcp/jira", map[string]string{"Authorization": "Bearer boss_mcpgw_W"})
		if status != http.StatusForbidden || r.lastCall().RejectedBy != "route_denied" {
			t.Fatalf("%s /mcp/jira: %d %+v", m, status, r.lastCall())
		}
	}
	if len(r.seen) != before {
		t.Fatal("a denied method must not be forwarded")
	}
}

// ---- S13: host-held keys and per-destination tokens ------------------------

// fakeMinter answers like POST /v1/run-plane/runs/{run}/tokens: one token per
// audience, each naming its audience and the jkt it was minted for.
type fakeMinter struct {
	calls atomic.Int32
	fail  bool
	ttl   int
}

func (m *fakeMinter) mint(_ context.Context, a hostapi.Assignment, req hostapi.RunTokensRequest) (*hostapi.RunTokensResponse, error) {
	m.calls.Add(1)
	if m.fail {
		return nil, errors.New("409 fenced")
	}
	out := &hostapi.RunTokensResponse{RunID: a.RunID, JKT: req.JKT, Tokens: map[string]hostapi.IssuedToken{}, ExpiresIn: m.ttl}
	for _, aud := range req.Audiences {
		out.Tokens[aud] = hostapi.IssuedToken{AccessToken: "tok." + aud + "." + req.JKT, ExpiresIn: m.ttl, JTI: "rt_" + aud}
	}
	return out, nil
}

func TestEachDestinationGetsOnlyItsOwnTokenWithAHostProof(t *testing.T) {
	r := newRig(t)
	bindWorkItem(r)
	m := &fakeMinter{ttl: 300}
	r.gw.Keys = &RunKeys{Mint: m.mint}
	a := hostapi.Assignment{RunID: "run_9", JobID: "job_9", ReplicaID: "rep_a", Epoch: 4, RunToken: "lease_SECRET"}
	if err := r.gw.Keys.Ensure(context.Background(), a); err != nil {
		t.Fatal(err)
	}
	jkt := r.gw.Keys.JKT("run_9")
	if jkt == "" || m.calls.Load() != 1 {
		t.Fatalf("one mint with a jkt expected; jkt=%q calls=%d", jkt, m.calls.Load())
	}

	// The MCP forward carries the boss:mcp token and a proof over the forwarded URL.
	if status, _ := r.call("vm-a", guest.PortMCP, "POST", "/mcp/jira", map[string]string{"Authorization": "Bearer boss_mcpgw_W"}); status != 200 {
		t.Fatalf("mcp forward: %d %+v", status, r.lastCall())
	}
	h := r.seen[len(r.seen)-1]
	tok, proof := h.Get(hostapi.HeaderDelegation), h.Get(hostapi.HeaderDPoP)
	if tok != "tok.boss:mcp."+jkt {
		t.Fatalf("the MCP forward must carry the boss:mcp token only, got %q", tok)
	}
	htu := NormalizeHTU("https", strings.TrimPrefix(r.up.URL, "https://"), "/mcp/jira")
	got, err := VerifyProof(proof, "POST", htu, tok, time.Now())
	if err != nil || got != jkt {
		t.Fatalf("proof over %s: jkt=%q err=%v", htu, got, err)
	}
	if _, err := VerifyProof(proof, "POST", htu, "tok.boss:model."+jkt, time.Now()); err == nil {
		t.Fatal("a proof is bound to its token (ath): the model token must not pass with the MCP proof")
	}

	// The model forward carries the boss:model token: never the MCP one.
	if status, _ := r.call("vm-a", guest.PortModel, "POST", "/anthropic/v1/messages", map[string]string{"X-Api-Key": "boss_mdl_W"}); status != 200 {
		t.Fatalf("model forward: %d %+v", status, r.lastCall())
	}
	h = r.seen[len(r.seen)-1]
	if h.Get(hostapi.HeaderDelegation) != "tok.boss:model."+jkt {
		t.Fatalf("the model forward must carry the boss:model token only, got %q", h.Get(hostapi.HeaderDelegation))
	}
	// A run with no tokens yet forwards without a delegation header (never a half-made one).
	if status, _ := r.call("vm-a", guest.PortModel, "POST", "/anthropic/v1/messages", map[string]string{"X-Api-Key": "boss_mdl_A"}); status != 200 {
		t.Fatalf("model forward for run_7: %d", status)
	}
	h = r.seen[len(r.seen)-1]
	if h.Get(hostapi.HeaderDelegation) != "" || h.Get(hostapi.HeaderDPoP) != "" {
		t.Fatal("no tokens for run_7: nothing must be attached")
	}
}

func TestRotationAtTwoThirdsKeepsAnInFlightPairValid(t *testing.T) {
	m := &fakeMinter{ttl: 300}
	now := time.Unix(1_800_000_000, 0)
	keys := &RunKeys{Mint: m.mint, Now: func() time.Time { return now }}
	a := hostapi.Assignment{RunID: "run_9", JobID: "job_9", ReplicaID: "rep_a", Epoch: 4}
	if err := keys.Ensure(context.Background(), a); err != nil {
		t.Fatal(err)
	}
	first, ok := keys.Pair("run_9", hostapi.AudMCP)
	if !ok {
		t.Fatal("no pair after Ensure")
	}
	// A forward in flight captured `first`; it signs its proof later than the swap.
	keys.RotateDue(context.Background())
	if m.calls.Load() != 1 {
		t.Fatal("rotation before two thirds of the TTL")
	}
	now = now.Add(201 * time.Second) // past 2/3 of 300 s
	keys.RotateDue(context.Background())
	if m.calls.Load() != 2 {
		t.Fatalf("rotation due at two thirds: mints=%d", m.calls.Load())
	}
	second, _ := keys.Pair("run_9", hostapi.AudMCP)
	if second.JKT == first.JKT || second.Token == first.Token {
		t.Fatal("rotation must bring a NEW key and new tokens")
	}
	// The in-flight pair still produces a proof that verifies for ITS token and ITS key.
	hs, err := first.Headers("POST", "https://cp.example/mcp/jira", now)
	if err != nil {
		t.Fatal(err)
	}
	if jkt, err := VerifyProof(hs[hostapi.HeaderDPoP], "POST", "https://cp.example/mcp/jira", hs[hostapi.HeaderDelegation], now); err != nil || jkt != first.JKT {
		t.Fatalf("in-flight pair broken by rotation: jkt=%q err=%v", jkt, err)
	}
	if strings.HasSuffix(hs[hostapi.HeaderDelegation], second.JKT) {
		t.Fatal("the in-flight pair must keep the token minted for its own key")
	}
	// Expiry without a successful rotation: no pair is offered (the gateway forwards with the host proof alone).
	m.fail = true
	now = now.Add(300 * time.Second)
	keys.RotateDue(context.Background())
	if _, ok := keys.Pair("run_9", hostapi.AudMCP); ok {
		t.Fatal("expired tokens must not be attached")
	}
	keys.Forget("run_9")
	if keys.Len() != 0 {
		t.Fatal("Forget leaves nothing")
	}
}

func TestDPoPProofWithTheWrongKeyIsRefused(t *testing.T) {
	right, _ := NewDPoPKey()
	wrong, _ := NewDPoPKey()
	now := time.Now()
	token := "tok.boss:model." + right.JKT()
	proof, err := wrong.Proof("POST", "https://gw.example/anthropic/v1/messages", token, now)
	if err != nil {
		t.Fatal(err)
	}
	jkt, err := VerifyProof(proof, "POST", "https://gw.example/anthropic/v1/messages", token, now)
	if err != nil {
		t.Fatalf("a well-formed proof verifies under its own key: %v", err)
	}
	if jkt == right.JKT() {
		t.Fatal("the proof's key must not be the token's cnf.jkt: the gateway refuses it (dpop_key_mismatch)")
	}
	// Tampering: a proof re-targeted at another URL or method fails.
	if _, err := VerifyProof(proof, "GET", "https://gw.example/anthropic/v1/messages", token, now); err == nil {
		t.Fatal("htm must be checked")
	}
	if _, err := VerifyProof(proof, "POST", "https://gw.example/mcp/jira", token, now); err == nil {
		t.Fatal("htu must be checked")
	}
	if _, err := VerifyProof(proof, "POST", "https://gw.example/anthropic/v1/messages", token, now.Add(5*time.Minute)); err == nil {
		t.Fatal("a stale proof must be refused")
	}
	parts := strings.Split(proof, ".")
	if _, err := VerifyProof(parts[0]+"."+parts[1]+".AAAA"+parts[2][4:], "POST", "https://gw.example/anthropic/v1/messages", token, now); err == nil {
		t.Fatal("a forged signature must be refused")
	}
}

// RFC 8037 appendix A.3: the thumbprint of the example Ed25519 key is exactly
// what engine/boss_engine/identity/jwks.thumbprint computes.
func TestThumbprintMatchesRFC8037Vector(t *testing.T) {
	x, err := base64.RawURLEncoding.DecodeString("11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo")
	if err != nil {
		t.Fatal(err)
	}
	if got := Thumbprint(ed25519.PublicKey(x)); got != "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k" {
		t.Fatalf("thumbprint = %s", got)
	}
	if NormalizeHTU("HTTPS", "GW.Example:443", "/mcp/jira?x=1") != "https://gw.example/mcp/jira" {
		t.Fatal("htu normalisation")
	}
	if NormalizeHTU("http", "127.0.0.1:8092", "") != "http://127.0.0.1:8092/" {
		t.Fatal("non-default port kept")
	}
}

func TestEnsureSurvivesAMintFailureAndNeverLogsTheKey(t *testing.T) {
	m := &fakeMinter{ttl: 300, fail: true}
	var logged []string
	keys := &RunKeys{Mint: m.mint, Log: func(f string, a ...any) { logged = append(logged, f) }}
	a := hostapi.Assignment{RunID: "run_9"}
	if err := keys.Ensure(context.Background(), a); err == nil {
		t.Fatal("a refused mint is an error")
	}
	if _, ok := keys.Pair("run_9", hostapi.AudMCP); ok {
		t.Fatal("no pair after a failed mint")
	}
	m.fail = false
	if err := keys.Ensure(context.Background(), a); err != nil {
		t.Fatal(err)
	}
	if _, ok := keys.Pair("run_9", hostapi.AudRunPlane); !ok {
		t.Fatal("the next Ensure succeeds")
	}
	for _, l := range logged {
		if strings.Contains(l, "tok.") {
			t.Fatal("a token reached the log")
		}
	}
}

// Review HIGH (PR #121): a guest may never reach the host's token-minting
// call. If it could, it would POST its OWN jkt and the gateway would attach
// the run token and the host proof, handing the guest all four tokens bound
// to a key it holds.
func TestAGuestCannotMintTokensThroughTheGateway(t *testing.T) {
	r := newRig(t)
	bindWorkItem(r)
	r.gw.Runs.Put("run_9", RunLease{ReplicaID: "rep_a", JobID: "job_9", Token: "lease_SECRET", Epoch: 4})
	r.gw.Runs.Put("run_b", RunLease{ReplicaID: "rep_b", JobID: "job_b", Token: "lease_B", Epoch: 1})
	before := len(r.seen)
	for _, c := range []struct{ vm, path string }{
		{"vm-a", "/v1/run-plane/runs/run_9/tokens"},
		{"vm-a", "/v1/run-plane/runs/run_9/tokens/"},
		{"vm-a", "/v1/run-plane/runs/run_9/TOKENS"},
		{"vm-a", "/v1/run-plane/runs/run_9/tokens/rotate"},
		{"vm-b", "/v1/run-plane/runs/run_b/tokens"}, // a replica, by its socket alone
	} {
		status, _ := r.call(c.vm, guest.PortRunPlane, "POST", c.path, map[string]string{
			"Authorization": "Bearer boss_run_W", "Content-Type": "application/json"})
		if status != http.StatusForbidden || r.lastCall().RejectedBy != "route_denied" || r.lastCall().RuleID != "run-plane-tokens-denied" {
			t.Fatalf("%s %s: %d %+v", c.vm, c.path, status, r.lastCall())
		}
	}
	if len(r.seen) != before {
		t.Fatal("a token-minting call from a guest must never be forwarded")
	}
	// Ordinary run calls still go through.
	if status, _ := r.call("vm-b", guest.PortRunPlane, "GET", "/v1/run-plane/runs/run_b", nil); status != 200 {
		t.Fatalf("run package: %d", status)
	}
	for _, p := range []string{"/v1/run-plane/runs/run_9/tokensX", "/v1/run-plane/runs/tokens", "/v1/run-plane/runs/run_9/lease"} {
		if TokenMintingPath(p) {
			t.Fatalf("%s is not a minting path", p)
		}
	}
}

// Review MED (PR #121): an admitted run's tokens are minted BEFORE the guest
// sees the lease; a refused mint answers 503 tokens_unavailable and the host
// forgets the run token, so nothing runs without its tokens.
func TestAnAdmittedRunGetsItsTokensFirstOrIsRefused(t *testing.T) {
	r := newRig(t)
	m := &fakeMinter{ttl: 300}
	r.gw.Keys = &RunKeys{Mint: m.mint}
	admit := func() (int, string) {
		return r.call("vm-b", guest.PortRunPlane, "POST", "/v1/run-plane/admit", map[string]string{"Content-Type": "application/json"})
	}
	if status, body := admit(); status != 200 || strings.Contains(body, "lease_SECRET_adm") {
		t.Fatalf("admit: %d %s", status, body)
	}
	if _, ok := r.gw.Keys.Pair("run_adm", hostapi.AudRunPlane); !ok || m.calls.Load() != 1 {
		t.Fatal("the admitted run's tokens must exist when the guest gets the answer")
	}
	m.fail = true
	r.gw.Keys.Forget("run_adm")
	status, body := admit()
	if status != http.StatusServiceUnavailable || !strings.Contains(body, "tokens_unavailable") || strings.Contains(body, "lease_SECRET_adm") {
		t.Fatalf("refused mint: %d %s", status, body)
	}
	if _, ok := r.gw.Runs.Get("run_adm"); ok {
		t.Fatal("a run without its tokens must not stay in the host's run map")
	}
	// A control plane that predates S13 (404): the run goes ahead without tokens.
	r.gw.MintNotSupported = func(error) bool { return true }
	if status, _ := admit(); status != 200 {
		t.Fatalf("old control plane: %d", status)
	}
}

// Re-review LOW (PR #121): no spelling of the minting path reaches the control
// plane — upper-case prefix, a path parameter, encoded whitespace.
func TestNoSpellingOfTheMintingPathIsForwarded(t *testing.T) {
	r := newRig(t)
	r.gw.Runs.Put("run_b", RunLease{ReplicaID: "rep_b", JobID: "job_b", Token: "lease_B", Epoch: 1})
	before := len(r.seen)
	for _, p := range []string{
		"/V1/RUN-PLANE/RUNS/run_b/TOKENS",
		"/v1/run-plane/RUNS/run_b/tokens",
		"/v1/run-plane/runs/run_b/tokens;a",
		"/v1/run-plane/runs/run_b/tokens%20",
		"/v1/run-plane/runs/run_b/tokens%3Ba",
		"/v1/run-plane/runs/run_b/tokens%09",
	} {
		status, _ := r.call("vm-b", guest.PortRunPlane, "POST", p, map[string]string{"Content-Type": "application/json"})
		if status != http.StatusForbidden && status != http.StatusBadRequest {
			t.Fatalf("%s: %d %+v", p, status, r.lastCall())
		}
		if why := r.lastCall().RejectedBy; why != "route_denied" && why != "path_not_canonical" {
			t.Fatalf("%s refused for %q", p, why)
		}
	}
	if len(r.seen) != before {
		t.Fatal("a spelling of the minting path was forwarded")
	}
	if !TokenMintingPath("/V1/RUN-PLANE/RUNS/x/TOKENS") || TokenMintingPath("/v1/run-plane/runs/x/lease") {
		t.Fatal("TokenMintingPath must fold the whole path")
	}
	for _, p := range []string{"/a;b", "/a b", "/a%20b", "/a%3bb", "/a\tb"} {
		if CleanPath(strings.ReplaceAll(p, "%20", " "), p) {
			t.Fatalf("CleanPath accepted %q", p)
		}
	}
	if !CleanPath("/v1/run-plane/runs/run_b/lease", "/v1/run-plane/runs/run_b/lease") {
		t.Fatal("an ordinary path must stay clean")
	}
}
