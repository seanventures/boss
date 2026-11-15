// Package gateway is the guest gateway skeleton in boss-hostd (LLD §3.8): the
// only exit for guest-originated traffic. One set of listeners per VM (vsock
// Unix sockets under Firecracker, plain Unix sockets for the process backend),
// so the listener that accepted a connection IS the VM's identity.
//
// Per request: identify (by listener) → match the route table → resolve the
// handle to (run, epoch, vm) → strip every guest credential header → forward
// over mTLS with the VM's host-held SVID plus a signed run assertion → audit.
// There is no fallback path: a missing endpoint or an unreachable gateway is a
// visible error.
package gateway

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/guest"
	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanheiney/boss/services/sandbox-service/internal/pki"
)

// MaxConnsPerVM is the per-VM connection cap (LLD §3.6).
const MaxConnsPerVM = 256

// Binding is what a handle (or, for a replica's run-plane calls, the socket)
// resolves to. Bindings live in memory only.
type Binding struct {
	Kind         string
	VMID         string
	ReplicaID    string
	DeploymentID string
	RunID        string
	Epoch        int64
}

// VMInfo carries the SVID claims for a VM.
type VMInfo struct {
	DeploymentID      string
	DeploymentVersion string
	ImageDigest       string
}

// Fingerprint is the audit-safe form of a handle (never the handle).
func Fingerprint(handle string) string {
	sum := sha256.Sum256([]byte(handle))
	return hex.EncodeToString(sum[:])[:16]
}

type vmGW struct {
	id        string
	replica   Binding // the VM's own identity (Kind replica for replica VMs)
	info      VMInfo
	listeners []net.Listener
	servers   []*http.Server
	transport *http.Transport
	conns     atomic.Int64
}

// Gateway serves every VM on the host.
type Gateway struct {
	HostID      string
	Minter      *pki.Minter
	UpstreamCAs *x509.CertPool // pinned CP CA bundle + fleet CA: upstreams must chain to them
	// CPBase resolves relative endpoints ("/v1/run-plane") against the CP URL.
	CPBase *url.URL
	// HostProof signs a BossHost proof; HostCert is the host's TLS client cert.
	HostProof func(method, path string) (string, error)
	HostCert  func() *tls.Certificate
	Rules     []Rule
	// Runs is the host's run → lease map (the run token stays here).
	Runs *RunMap
	// Keys holds the per-run DPoP keys and delegated tokens (P8b S13); nil
	// forwards without X-Boss-Delegation (the gateways then rely on the
	// host proof alone, until BOSS_HOST_TOKENS_REQUIRED).
	Keys *RunKeys
	// MintNotSupported classifies a mint error as "the control plane has no
	// tokens endpoint" (cpclient.IsNotFound), so the run proceeds without.
	MintNotSupported func(error) bool
	Audit            func(hostapi.GuestCall)
	Event            func(hostapi.HostEvent)
	// Enabled gates forwarding (false while the host is revoked).
	Enabled func() bool

	mu       sync.RWMutex
	bindings map[string]Binding // handle → binding (desired state)
	// runBindings are the handles that arrived WITH a claimed run (hosts/next
	// Assignment.Handles, HOST_API.md §2.8): bound here to (run, epoch, the
	// delivering VM) until the lease ends. Kept apart from `bindings`, which a
	// desired-state Update rebuilds.
	runBindings map[string]Binding
	runHandles  map[string][]string // run → its handles, for UnbindRun
	fences      map[string]int64    // run → min epoch
	revoked     map[string]bool     // handle fingerprint → revoked
	endpoints   map[string]string
	vmIdent     map[string]Binding // vm id → its replica identity
	vms         map[string]*vmGW
	hostTr      *http.Transport
}

func New(hostID string, minter *pki.Minter, cas *x509.CertPool) *Gateway {
	return &Gateway{
		HostID: hostID, Minter: minter, UpstreamCAs: cas, Rules: DefaultRules(),
		bindings: map[string]Binding{}, fences: map[string]int64{}, revoked: map[string]bool{},
		runBindings: map[string]Binding{}, runHandles: map[string][]string{},
		vms: map[string]*vmGW{}, vmIdent: map[string]Binding{}, endpoints: map[string]string{},
		Runs: NewRunMap(),
	}
}

// handleKinds maps the names a work item's `handles` uses (contract v1.1:
// mdl, egr, mcpgw, run) and the desired-state names (model, egress, mcp,
// run) to the gateway's handle kinds.
var handleKinds = map[string]string{
	"mdl": HandleModel, "model": HandleModel,
	"egr": HandleEgress, "egress": HandleEgress,
	"mcpgw": HandleMCP, "mcp": HandleMCP,
	"run": HandleRun,
}

// BindRun binds the handles that arrived with a claimed run to (run, epoch,
// the VM that takes it). Non-string entries (mcp_servers) are not handles
// and are ignored. A later BindRun for the same run replaces the first.
func (g *Gateway) BindRun(vmID, replicaID, deploymentID, runID string, epoch int64, handles map[string]any) {
	g.mu.Lock()
	defer g.mu.Unlock()
	for _, h := range g.runHandles[runID] {
		delete(g.runBindings, h)
	}
	var bound []string
	for name, v := range handles {
		kind, known := handleKinds[name]
		raw, isStr := v.(string)
		if !known || !isStr || raw == "" {
			continue
		}
		g.runBindings[raw] = Binding{Kind: kind, VMID: vmID, ReplicaID: replicaID, DeploymentID: deploymentID, RunID: runID, Epoch: epoch}
		bound = append(bound, raw)
	}
	g.runHandles[runID] = bound
}

// UnbindRun forgets a run's handles (its lease ended or was handed back).
func (g *Gateway) UnbindRun(runID string) {
	g.mu.Lock()
	for _, h := range g.runHandles[runID] {
		delete(g.runBindings, h)
	}
	delete(g.runHandles, runID)
	g.mu.Unlock()
	if g.Keys != nil {
		g.Keys.Forget(runID)
	}
}

// RunBound reports how many handles a run has bound (tests).
func (g *Gateway) RunBound(runID string) int {
	g.mu.RLock()
	defer g.mu.RUnlock()
	return len(g.runHandles[runID])
}

// Update applies the desired state's bindings, fences, endpoints and
// revocations (level-based: bindings are rebuilt; local revocations persist).
func (g *Gateway) Update(ds hostapi.DesiredState) {
	b := map[string]Binding{}
	ident := map[string]Binding{}
	for _, vm := range ds.VMs {
		self := Binding{Kind: HandleReplica, VMID: vm.VMID, ReplicaID: vm.ReplicaID, DeploymentID: vm.DeploymentID}
		if vm.Mode == hostapi.ModeReplica {
			ident[vm.VMID] = self
		} else {
			ident[vm.VMID] = Binding{Kind: "per_run", VMID: vm.VMID, ReplicaID: vm.ReplicaID, DeploymentID: vm.DeploymentID}
		}
		if vm.ReplicaHandle != "" {
			b[vm.ReplicaHandle] = self
		}
		for _, run := range vm.Runs {
			add := func(kind, h string) {
				if h != "" {
					b[h] = Binding{Kind: kind, VMID: vm.VMID, ReplicaID: vm.ReplicaID, DeploymentID: vm.DeploymentID, RunID: run.RunID, Epoch: run.Epoch}
				}
			}
			add(HandleModel, run.Handles.Model)
			add(HandleEgress, run.Handles.Egress)
			add(HandleMCP, run.Handles.MCP)
			add(HandleRun, run.Handles.Run)
		}
	}
	f := map[string]int64{}
	for _, fe := range ds.Fences {
		f[fe.RunID] = fe.MinEpoch
	}
	ep := map[string]string{}
	for k, v := range ds.Endpoints {
		ep[k] = v
	}
	g.mu.Lock()
	g.bindings, g.fences, g.endpoints, g.vmIdent = b, f, ep, ident
	for _, fp := range ds.Revocations {
		g.revoked[fp] = true
	}
	g.mu.Unlock()
}

// Attach starts one listener per relay port for a VM. listen is the VM
// manager's GuestListen.
func (g *Gateway) Attach(vmID string, info VMInfo, listen func(port int) (net.Listener, error)) error {
	g.mu.Lock()
	if _, ok := g.vms[vmID]; ok {
		g.mu.Unlock()
		return nil
	}
	v := &vmGW{id: vmID, info: info}
	g.vms[vmID] = v
	g.mu.Unlock()
	v.transport = g.transportFor(v)
	for _, port := range guest.RelayPorts {
		ln, err := listen(port)
		if err != nil {
			g.Detach(vmID)
			return err
		}
		port := port
		srv := &http.Server{
			Handler:           http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { g.serve(v, port, w, r) }),
			ReadHeaderTimeout: 10 * time.Second,
			ConnState: func(_ net.Conn, s http.ConnState) {
				switch s {
				case http.StateNew:
					v.conns.Add(1)
				case http.StateClosed, http.StateHijacked:
					v.conns.Add(-1)
				}
			},
		}
		v.listeners = append(v.listeners, ln)
		v.servers = append(v.servers, srv)
		go func() { _ = srv.Serve(ln) }()
	}
	return nil
}

// Detach closes a VM's listeners and forgets its SVID.
func (g *Gateway) Detach(vmID string) {
	g.mu.Lock()
	v, ok := g.vms[vmID]
	delete(g.vms, vmID)
	g.mu.Unlock()
	if !ok {
		return
	}
	for _, s := range v.servers {
		_ = s.Close()
	}
	for _, ln := range v.listeners {
		_ = ln.Close()
	}
	if v.transport != nil {
		v.transport.CloseIdleConnections()
	}
	if g.Minter != nil {
		g.Minter.Forget(vmID)
	}
}

// Attached reports whether a VM has gateway listeners.
func (g *Gateway) Attached(vmID string) bool {
	g.mu.RLock()
	defer g.mu.RUnlock()
	_, ok := g.vms[vmID]
	return ok
}

func (g *Gateway) svid(v *vmGW) (*pki.SVID, error) {
	if g.Minter == nil {
		return nil, errors.New("no SVID minter")
	}
	return g.Minter.For(v.id, pki.SVIDClaims{DeploymentID: v.info.DeploymentID,
		DeploymentVersion: v.info.DeploymentVersion, ImageDigest: v.info.ImageDigest})
}

func (g *Gateway) transportFor(v *vmGW) *http.Transport {
	return &http.Transport{
		TLSClientConfig: &tls.Config{
			RootCAs:    g.UpstreamCAs,
			MinVersion: tls.VersionTLS12,
			GetClientCertificate: func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
				s, err := g.svid(v)
				if err != nil {
					return nil, err
				}
				c := s.TLSCertificate()
				return &c, nil
			},
		},
		ForceAttemptHTTP2:     true,
		MaxIdleConnsPerHost:   8,
		IdleConnTimeout:       60 * time.Second,
		ResponseHeaderTimeout: 0, // streams: no header timeout beyond the upstream's own
	}
}

type denial struct {
	status int
	reason string
}

func (g *Gateway) resolve(v *vmGW, rule Rule, handle string) (Binding, *denial) {
	if handle == "" {
		g.mu.RLock()
		self, ok := g.vmIdent[v.id]
		g.mu.RUnlock()
		if rule.SocketIdentity && ok && self.Kind == HandleReplica {
			return self, nil // the socket is the replica
		}
		return Binding{}, &denial{http.StatusUnauthorized, "handle_required"}
	}
	fp := Fingerprint(handle)
	g.mu.RLock()
	b, ok := g.bindings[handle]
	if !ok {
		b, ok = g.runBindings[handle]
	}
	revoked := g.revoked[fp]
	minEpoch, fenced := g.fences[b.RunID]
	g.mu.RUnlock()
	switch {
	case revoked:
		return b, &denial{http.StatusForbidden, "handle_revoked"}
	case !ok:
		return b, &denial{http.StatusForbidden, "handle_unknown"}
	case b.VMID != v.id:
		// A handle presented from another VM's socket: refuse, revoke, report.
		g.mu.Lock()
		g.revoked[fp] = true
		g.mu.Unlock()
		if g.Event != nil {
			g.Event(hostapi.HostEvent{At: hostapi.Now(), VMID: v.id, Kind: "handle_replay",
				Actor: "gateway", Detail: map[string]string{"handle_fingerprint": fp, "bound_vm": b.VMID}})
		}
		return b, &denial{http.StatusForbidden, "handle_replay"}
	case !kindAllowed(rule.Handle, b.Kind):
		return b, &denial{http.StatusForbidden, "handle_wrong_route"}
	case fenced && b.Epoch < minEpoch:
		return b, &denial{http.StatusConflict, "fenced"}
	}
	return b, nil
}

func kindAllowed(want, have string) bool {
	if want == HandleRun {
		return have == HandleRun || have == HandleReplica
	}
	return want == have
}

type countingWriter struct {
	http.ResponseWriter
	status int
	n      int64
}

func (c *countingWriter) WriteHeader(code int) { c.status = code; c.ResponseWriter.WriteHeader(code) }
func (c *countingWriter) Write(p []byte) (int, error) {
	if c.status == 0 {
		c.status = http.StatusOK
	}
	n, err := c.ResponseWriter.Write(p)
	c.n += int64(n)
	return n, err
}
func (c *countingWriter) Flush() {
	if f, ok := c.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

type countingBody struct {
	io.ReadCloser
	n *int64
}

func (c countingBody) Read(p []byte) (int, error) {
	n, err := c.ReadCloser.Read(p)
	atomic.AddInt64(c.n, int64(n))
	return n, err
}

func (g *Gateway) serve(v *vmGW, port int, w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	cw := &countingWriter{ResponseWriter: w}
	var bytesOut int64
	rule := match(g.Rules, port, r.Method, r.URL.Path)
	handle := handleFrom(r.Header)
	call := hostapi.GuestCall{
		CallID: newCallID(), At: start.UTC().Format(time.RFC3339Nano), VMID: v.id,
		Route: auditRoute(rule.Route, port), Method: r.Method, PathTemplate: pathTemplate(r.URL.Path), RuleID: rule.ID,
		DeploymentID: v.info.DeploymentID, TraceID: traceID(r.Header.Get("traceparent")),
	}
	if handle != "" {
		call.HandleFP = Fingerprint(handle)
	}
	finish := func() {
		call.Status = cw.status
		call.BytesIn = cw.n
		call.BytesOut = atomic.LoadInt64(&bytesOut)
		call.DurationMS = time.Since(start).Milliseconds()
		if g.Audit != nil {
			g.Audit(call)
		}
	}
	defer finish()
	deny := func(status int, reason string) {
		call.Verdict, call.RejectedBy = "deny", reason
		writeErr(cw, status, reason)
	}
	if v.conns.Load() > MaxConnsPerVM {
		deny(http.StatusTooManyRequests, "vm_connection_cap")
		return
	}
	if g.Enabled != nil && !g.Enabled() {
		deny(http.StatusServiceUnavailable, "host_not_serving")
		return
	}
	if !CleanPath(r.URL.Path, r.URL.EscapedPath()) {
		deny(http.StatusBadRequest, "path_not_canonical")
		return
	}
	if rule.Deny {
		deny(http.StatusForbidden, "route_denied")
		return
	}
	if rule.Credential {
		g.serveCredential(v, cw, r, &call)
		return
	}
	if rule.Challenge && handle == "" {
		cw.Header().Set("WWW-Authenticate", `Basic realm="boss"`)
		deny(http.StatusUnauthorized, "handle_required")
		return
	}
	b, d := g.resolve(v, rule, handle)
	if d != nil {
		deny(d.status, d.reason)
		return
	}
	call.RunID = b.RunID
	if b.RunID != "" {
		ep := b.Epoch
		call.Epoch = &ep
	}
	g.mu.RLock()
	base := g.endpoints[rule.Endpoint]
	g.mu.RUnlock()
	up, err := g.resolveEndpoint(base)
	if err != nil {
		deny(http.StatusServiceUnavailable, "gateway_unconfigured")
		return
	}
	call.DestHost = up.Host
	transport := v.transport
	var setAuth func(h http.Header, method, path string) error
	if rule.HostAuth {
		transport = g.hostTransport()
	}
	if rule.HostAuth || rule.HostProof {
		setAuth = func(h http.Header, method, path string) error {
			if g.HostProof == nil {
				return errors.New("no host proof")
			}
			proof, err := g.HostProof(method, path)
			if err != nil {
				return err
			}
			h.Set("Authorization", hostapi.AuthScheme+" "+proof)
			if rule.HostAuth {
				h.Set("X-Boss-Replica", b.ReplicaID) // the run plane's identity header
			}
			return nil
		}
	}
	svid, err := g.svid(v)
	if err != nil {
		deny(http.StatusServiceUnavailable, "svid_unavailable")
		return
	}
	assertion, err := SignAssertion(svid.Key, newAssertion(svid.ID, rule.Route, b, time.Now()))
	if err != nil {
		deny(http.StatusInternalServerError, "assertion_failed")
		return
	}
	call.Verdict = "allow"
	if r.Body != nil {
		r.Body = countingBody{ReadCloser: r.Body, n: &bytesOut}
	}
	var keep []string
	var runToken, leaseRun string
	ends := false
	if rule.Route == RouteRunPlane {
		keep = LeaseHeaders // X-Boss-Job, X-Boss-Fence-Epoch (the run token is the host's)
		if leaseRun = runOfPath(r.URL.Path); leaseRun != "" {
			l, ok := g.Runs.Get(leaseRun)
			if !ok || l.ReplicaID != b.ReplicaID {
				deny(http.StatusConflict, "fenced")
				return
			}
			runToken = l.Token
			call.RunID = leaseRun
			ep := l.Epoch
			call.Epoch = &ep
			ends = leaseEnds(r)
		}
	}
	// S13: the run's delegated token for THIS destination, with the host's
	// proof. The (token, key) pair is captured here, once per request, so a
	// rotation between now and the proof never pairs a new key with an old
	// token; the previous pair stays valid until it expires.
	var pair TokenPair
	havePair := false
	if g.Keys != nil && rule.Audience != "" {
		tokenRun := b.RunID
		if leaseRun != "" {
			tokenRun = leaseRun
		}
		if tokenRun != "" {
			pair, havePair = g.Keys.Pair(tokenRun, rule.Audience)
		}
	}
	var authErr error
	proxy := &httputil.ReverseProxy{
		Transport:     transport,
		FlushInterval: -1, // stream chunk by chunk (SSE)
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.Out.URL.Scheme = up.Scheme
			pr.Out.URL.Host = up.Host
			pr.Out.URL.Path = joinPath(up.Path, pr.In.URL.Path)
			pr.Out.URL.RawPath = ""
			pr.Out.Host = up.Host
			StripGuestHeaders(pr.Out.Header, keep...)
			pr.Out.Trailer = nil // guest trailers could smuggle headers past the strip
			pr.Out.Header.Set(AssertionHeader, assertion)
			if rule.HostProof {
				// The guest's handle, MOVED out of its credential header, and the host's
				// word on which VM, run and epoch the call came from (HOST_API.md §4.6).
				// The far gateway binds the handle to exactly these.
				pr.Out.Header.Set("X-Boss-Handle", handle)
				pr.Out.Header.Set("X-Boss-Vm", v.id)
				if b.RunID != "" {
					pr.Out.Header.Set("X-Boss-Run", b.RunID)
					pr.Out.Header.Set("X-Boss-Fence-Epoch", strconv.FormatInt(b.Epoch, 10))
				}
				if b.DeploymentID != "" {
					pr.Out.Header.Set("X-Boss-Deployment", b.DeploymentID)
				}
			}
			if runToken != "" {
				pr.Out.Header.Set("X-Boss-Run-Token", runToken)
			}
			if havePair {
				htu := NormalizeHTU(pr.Out.URL.Scheme, pr.Out.URL.Host, pr.Out.URL.Path)
				if hs, err := pair.Headers(pr.Out.Method, htu, time.Now()); err == nil {
					for k, v := range hs {
						pr.Out.Header.Set(k, v)
					}
				} else {
					authErr = err
					pr.Out.URL.Host = "invalid.invalid" // never forwarded with a half-made proof
				}
			}
			if setAuth != nil {
				authErr = setAuth(pr.Out.Header, pr.Out.Method, pr.Out.URL.Path)
				if authErr != nil {
					pr.Out.URL.Host = "invalid.invalid" // never forwarded unauthenticated
				}
			}
		},
		ModifyResponse: func(resp *http.Response) error {
			if rule.Route != RouteRunPlane {
				return nil
			}
			if ends && resp.StatusCode >= 200 && resp.StatusCode <= 299 {
				g.Runs.Delete(leaseRun) // the lease ended: forget the token
				g.UnbindRun(leaseRun)   // and the run's handles, keys and tokens (S13)
			}
			if r.URL.Path == "/v1/run-plane/admit" {
				return g.stripAdmitToken(resp, b.ReplicaID)
			}
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, err error) {
			// Fail closed and visibly: there is no direct path to retry on.
			call.RejectedBy = "upstream_unreachable"
			if authErr != nil {
				call.RejectedBy = "host_proof_failed"
			}
			writeErr(w, http.StatusBadGateway, call.RejectedBy)
		},
	}
	proxy.ServeHTTP(cw, r)
}

// serveCredential answers git's credential helper (`git credential fill`
// format) with the VM's OWN egress handle, from the socket's identity: never a
// token, never another VM's handle (P8b S11, HOST_API.md §4.9). A VM that holds
// several runs' handles names the run (`?run=`); a fenced or unknown run gets
// nothing.
func (g *Gateway) serveCredential(v *vmGW, w http.ResponseWriter, r *http.Request, call *hostapi.GuestCall) {
	want := r.URL.Query().Get("run")
	g.mu.RLock()
	var found []Binding
	var raws []string
	for _, m := range []map[string]Binding{g.bindings, g.runBindings} {
		for raw, b := range m {
			if b.Kind != HandleEgress || b.VMID != v.id || g.revoked[Fingerprint(raw)] {
				continue
			}
			if min, ok := g.fences[b.RunID]; ok && b.Epoch < min {
				continue
			}
			if want != "" && b.RunID != want {
				continue
			}
			found = append(found, b)
			raws = append(raws, raw)
		}
	}
	g.mu.RUnlock()
	if len(found) != 1 {
		reason := "no_egress_handle"
		if len(found) > 1 {
			reason = "run_required"
		}
		call.Verdict, call.RejectedBy = "deny", reason
		writeErr(w, http.StatusNotFound, reason)
		return
	}
	call.Verdict, call.RunID, call.HandleFP = "allow", found[0].RunID, Fingerprint(raws[0])
	ep := found[0].Epoch
	call.Epoch = &ep
	w.Header().Set("Content-Type", "text/plain")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	_, _ = io.WriteString(w, "username=boss\npassword="+raws[0]+"\n")
}

// joinPath prefixes the endpoint path unless the guest path already carries
// it (the run plane: endpoint "/v1/run-plane", guest path "/v1/run-plane/next").
func joinPath(base, p string) string {
	base = strings.TrimRight(base, "/")
	if base == "" || strings.HasPrefix(p, base+"/") || p == base {
		return p
	}
	return base + p
}

// resolveEndpoint turns a desired-state endpoint into an upstream URL.
// Relative paths resolve against the control plane.
func (g *Gateway) resolveEndpoint(base string) (*url.URL, error) {
	if base == "" {
		return nil, errors.New("no endpoint")
	}
	if strings.HasPrefix(base, "/") {
		if g.CPBase == nil {
			return nil, errors.New("relative endpoint without a control plane URL")
		}
		u := *g.CPBase
		u.Path = strings.TrimRight(u.Path, "/") + base
		return &u, nil
	}
	u, err := url.Parse(base)
	if err != nil || u.Host == "" || !upstreamAllowed(u) {
		return nil, errors.New("endpoint not allowed")
	}
	return u, nil
}

func (g *Gateway) hostTransport() *http.Transport {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.hostTr == nil {
		g.hostTr = &http.Transport{
			TLSClientConfig: &tls.Config{
				RootCAs:    g.UpstreamCAs,
				MinVersion: tls.VersionTLS12,
				GetClientCertificate: func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
					if g.HostCert != nil {
						if c := g.HostCert(); c != nil {
							return c, nil
						}
					}
					return &tls.Certificate{}, nil
				},
			},
			ForceAttemptHTTP2: true, MaxIdleConnsPerHost: 16, IdleConnTimeout: 60 * time.Second,
		}
	}
	return g.hostTr
}

// RotateHostTransport drops pooled host connections after a renewal.
func (g *Gateway) RotateHostTransport() {
	g.mu.Lock()
	old := g.hostTr
	g.hostTr = nil
	g.mu.Unlock()
	if old != nil {
		old.CloseIdleConnections()
	}
}

// auditRoute names the route for guest_calls; a default-deny is attributed to
// the port's route (the audit schema has no "deny" route; rejected_by says it).
func auditRoute(r string, port int) string {
	if r != "deny" {
		return r
	}
	switch port {
	case guest.PortModel:
		return RouteModel
	case guest.PortGit:
		return RouteGit
	case guest.PortMCP:
		return RouteMCP
	case guest.PortRunPlane:
		return RouteRunPlane
	}
	return RouteConnect
}

func newCallID() string {
	b := make([]byte, 12)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// upstreamAllowed requires TLS, except plain http to loopback (dev only).
func upstreamAllowed(u *url.URL) bool {
	if u.Scheme == "https" {
		return true
	}
	if u.Scheme != "http" {
		return false
	}
	ip := net.ParseIP(u.Hostname())
	return u.Hostname() == "localhost" || (ip != nil && ip.IsLoopback())
}

// pathTemplate keeps the audit row free of identifiers and query strings:
// only the first three path segments survive, the rest become "*".
func pathTemplate(p string) string {
	segs := strings.Split(strings.TrimPrefix(p, "/"), "/")
	if len(segs) > 3 {
		segs = append(segs[:3], "*")
	}
	return "/" + strings.Join(segs, "/")
}

func traceID(tp string) string {
	parts := strings.Split(tp, "-")
	if len(parts) == 4 && len(parts[1]) == 32 {
		return parts[1]
	}
	return ""
}

func writeErr(w http.ResponseWriter, status int, reason string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": "boss guest gateway: " + reason, "reason": reason})
}

// ---- Audit batching ------------------------------------------------------

// Batcher buffers rows and flushes them in batches (LLD §2.15.6). When the
// control plane is down it keeps up to Cap rows and counts what it drops.
// Rows carry a sequence number, so a flush removes exactly the rows it sent
// even if older rows were dropped from the front meanwhile.
type Batcher[T any] struct {
	Flush    func(ctx context.Context, rows []T) error
	Every    time.Duration
	MaxBatch int
	Cap      int

	mu      sync.Mutex
	buf     []T
	head    uint64 // sequence number of buf[0]
	dropped int64
}

func (b *Batcher[T]) Add(row T) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.Cap > 0 && len(b.buf) >= b.Cap {
		b.buf = b.buf[1:]
		b.head++
		b.dropped++
	}
	b.buf = append(b.buf, row)
}

// Dropped is how many rows were discarded because the buffer was full.
func (b *Batcher[T]) Dropped() int64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.dropped
}

// Run flushes until ctx ends, then makes one last attempt.
func (b *Batcher[T]) Run(ctx context.Context) {
	every := b.Every
	if every <= 0 {
		every = time.Second
	}
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			fctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			b.flushOnce(fctx)
			cancel()
			return
		case <-t.C:
			b.flushOnce(ctx)
		}
	}
}

func (b *Batcher[T]) flushOnce(ctx context.Context) {
	for {
		b.mu.Lock()
		n := len(b.buf)
		if n == 0 {
			b.mu.Unlock()
			return
		}
		max := b.MaxBatch
		if max <= 0 {
			max = 200
		}
		if n > max {
			n = max
		}
		rows := append([]T(nil), b.buf[:n]...)
		end := b.head + uint64(n) // one past the last sent sequence
		b.mu.Unlock()
		if err := b.Flush(ctx, rows); err != nil {
			return // keep them; retry next tick
		}
		b.mu.Lock()
		if end > b.head {
			k := int(end - b.head) // sent rows still buffered (some may have been dropped)
			if k > len(b.buf) {
				k = len(b.buf)
			}
			b.buf = b.buf[k:]
			b.head += uint64(k)
		}
		b.mu.Unlock()
	}
}
