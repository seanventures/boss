// Package hostd is boss-hostd's fleet agent: it enrols the host, keeps its
// mTLS identity renewed, heartbeats capacity and hardening facts, watches its
// desired state, and reconciles VMs toward it with a watchdog (LLD §3.2,
// §3.3, §3.16.1). Postgres (behind the control plane) is the only truth; the
// bbolt cache and this process's memory are rebuilt from desired state plus
// /proc and netlink scans after any crash.
package hostd

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"log"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/cache"
	"github.com/seanheiney/boss/services/sandbox-service/internal/cpclient"
	"github.com/seanheiney/boss/services/sandbox-service/internal/gateway"
	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanheiney/boss/services/sandbox-service/internal/lbd"
	"github.com/seanheiney/boss/services/sandbox-service/internal/netpol"
	"github.com/seanheiney/boss/services/sandbox-service/internal/pki"
	"github.com/seanheiney/boss/services/sandbox-service/internal/vmm"
)

// Config is everything the agent needs. Zero durations take the LLD defaults.
type Config struct {
	StateDir string // pki/ and cache.db live here
	CPURL    string
	// PinnedCA is the PEM bundle that verifies the control plane's TLS
	// server certificate. It is the only trust root (no system roots).
	// Optional only for plain http to a loopback control plane (dev/CI).
	PinnedCA  []byte
	JoinToken string // used once, only when the host has no credentials yet
	// JoinTokenFile is deleted after a successful enrolment.
	JoinTokenFile string
	// HeadlessMax: with no control-plane contact for this long the host stops
	// serving (gateway refuses, no work is requested). Default 24 h (NFR-15).
	HeadlessMax time.Duration
	// RunHoldMS is passed to the P8a stand-in inbox (tests).
	RunHoldMS int
	// WorkWait is the hosts/next long-poll wait (default 25 s).
	WorkWait time.Duration
	// DeliverTimeout bounds a hand-off to a VM inbox (default 2 s).
	DeliverTimeout time.Duration
	Hostname       string
	Pool           string
	Labels         []string
	// Address is this host's reachable IPv4 (the directors route its
	// vm_prefix through it). Empty = detected toward the control plane.
	Address string

	Manager vmm.Manager
	Net     netpol.Manager

	// ClaimsFrozen is the local microvm_claims_frozen switch. per_run VMs are
	// refused while it (or the desired state's flag) is true. Replicas are the
	// activated mode and are not gated by it (operator decision 2026-11-14).
	ClaimsFrozen  bool
	StrictSecrets bool
	MaxVMs        int
	TapOwnerUID   int

	// RuntimeVersion is part of the snapshot key (the baked boss-runtime).
	RuntimeVersion string
	// ReplicaSurge is the warm spare kept per replica shape (0 = the default
	// of 1; -1 = none).
	ReplicaSurge int
	// SnapshotGCGrace: a snapshot no desired VM or pool references is deleted
	// once older than this (default 10 min). SnapshotGCEvery is how often the
	// store is swept (default 1 min).
	SnapshotGCGrace time.Duration
	SnapshotGCEvery time.Duration

	HeartbeatEvery time.Duration // 2 s
	WatchWait      time.Duration // 25 s long-poll
	ReconcileEvery time.Duration // 500 ms
	PingEvery      time.Duration // 500 ms
	PingMisses     int           // 3
	BootGrace      time.Duration // 15 s before missed pings count
	RestartMax     int           // 3
	RestartWindow  time.Duration // 10 min
	Logf           func(format string, args ...any)
}

func (c *Config) defaults() {
	if c.HeartbeatEvery <= 0 {
		c.HeartbeatEvery = 2 * time.Second
	}
	if c.WatchWait <= 0 {
		c.WatchWait = 25 * time.Second
	}
	if c.ReconcileEvery <= 0 {
		c.ReconcileEvery = 500 * time.Millisecond
	}
	if c.PingEvery <= 0 {
		c.PingEvery = 500 * time.Millisecond
	}
	if c.PingMisses <= 0 {
		c.PingMisses = 3
	}
	if c.BootGrace <= 0 {
		c.BootGrace = 15 * time.Second
	}
	if c.RestartMax <= 0 {
		c.RestartMax = 3
	}
	if c.RestartWindow <= 0 {
		c.RestartWindow = 10 * time.Minute
	}
	if c.MaxVMs <= 0 {
		c.MaxVMs = 20
	}
	if c.HeadlessMax <= 0 {
		c.HeadlessMax = 24 * time.Hour
	}
	if c.WorkWait <= 0 {
		c.WorkWait = 25 * time.Second
	}
	if c.DeliverTimeout <= 0 {
		c.DeliverTimeout = 2 * time.Second
	}
	if c.Net == nil {
		c.Net = netpol.Noop{}
	}
	if c.ReplicaSurge < 0 {
		c.ReplicaSurge = 0
	} else if c.ReplicaSurge == 0 {
		c.ReplicaSurge = 1
	}
	if c.SnapshotGCGrace <= 0 {
		c.SnapshotGCGrace = 10 * time.Minute
	}
	if c.SnapshotGCEvery <= 0 {
		c.SnapshotGCEvery = time.Minute
	}
	if c.Logf == nil {
		c.Logf = log.Printf
	}
	if c.Address == "" {
		c.Address = detectAddress(c.CPURL)
	}
}

// Agent is one running boss-hostd fleet agent.
type Agent struct {
	cfg    Config
	creds  atomic.Pointer[pki.HostCreds]
	pinned *x509.CertPool
	client *cpclient.Client
	cache  *cache.Cache
	minter *pki.Minter
	gw     *gateway.Gateway
	events *gateway.Batcher[hostapi.HostEvent]
	calls  *gateway.Batcher[hostapi.GuestCall]

	cpBase    *url.URL
	revoked   atomic.Bool
	noEvents  atomic.Bool
	lastCP    atomic.Int64 // unix nanos of the last successful CP call
	observed  atomic.Int64
	stateGen  atomic.Int64 // replica-state generation (bumped on every change)
	pushedGen atomic.Int64 // the generation the last successful /observed carried
	kick      chan struct{}
	renewNow  chan struct{}
	pushObs   chan struct{}
	unhealthy chan string

	mu          sync.Mutex
	desired     *hostapi.DesiredState
	desiredLive bool // false while running on the cached (handle-less) state
	vms         map[string]*vmState
	refused     map[string]refusal // vm id → reason (reported once per change)
	// gone holds stopped replicas, reported as state "stopped" until a push
	// carrying them succeeds (the control plane must see the stop).
	gone    map[string]hostapi.ReplicaStatus
	taps    map[string]bool // taps seen at start (orphan sweep)
	hbCount int

	// Director pushes (directors.go).
	pushMu  sync.Mutex
	push    *lbd.Pusher
	pushCA  string
	pushSeq atomic.Int64
	lanes   map[string]*memberLane

	// Warm pools (pool.go).
	warm         map[string]*warmVM    // slot id → warm VM
	filling      map[poolKey]int       // fills in flight
	snapshotting map[vmm.Shape]bool    // one snapshot at a time per shape
	poolBackoff  map[poolKey]time.Time // after a failed fill
	lastGC       time.Time
}

// New prepares an agent. Run does the work.
func New(cfg Config) (*Agent, error) {
	cfg.defaults()
	if cfg.Manager == nil {
		return nil, errors.New("hostd: a VM manager is required")
	}
	if cfg.CPURL == "" {
		return nil, errors.New("hostd: control plane URL is required (BOSS_HOSTD_CP_URL)")
	}
	base, err := url.Parse(strings.TrimRight(cfg.CPURL, "/"))
	if err != nil {
		return nil, fmt.Errorf("hostd: control plane URL: %w", err)
	}
	var pool *x509.CertPool
	if len(cfg.PinnedCA) > 0 {
		if pool, err = pki.PoolFromPEM(cfg.PinnedCA); err != nil {
			return nil, fmt.Errorf("hostd: pinned control-plane CA: %w", err)
		}
	}
	a := &Agent{cfg: cfg, pinned: pool, cpBase: base, kick: make(chan struct{}, 1),
		renewNow: make(chan struct{}, 1), pushObs: make(chan struct{}, 1), unhealthy: make(chan string, 64),
		vms: map[string]*vmState{}, refused: map[string]refusal{}, taps: map[string]bool{},
		gone: map[string]hostapi.ReplicaStatus{}, warm: map[string]*warmVM{}, filling: map[poolKey]int{},
		snapshotting: map[vmm.Shape]bool{}, poolBackoff: map[poolKey]time.Time{}}
	a.client, err = cpclient.New(cfg.CPURL, pool, func() *tls.Certificate {
		if c := a.creds.Load(); c != nil {
			cert := c.TLSCertificate()
			return &cert
		}
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("hostd: %w", err)
	}
	a.client.Prove = a.prove
	a.minter = pki.NewMinter(func() *pki.HostCreds { return a.creds.Load() })
	return a, nil
}

// prove signs a BossHost proof with the current host key.
func (a *Agent) prove(method, path string) (string, error) {
	c := a.creds.Load()
	if c == nil {
		return "", errors.New("not enrolled")
	}
	return pki.SignHostProof(c.ClientKey, c.ClientCert, method, path)
}

func (a *Agent) logf(format string, args ...any) { a.cfg.Logf("boss-hostd: "+format, args...) }

// HostID is empty until enrolled.
func (a *Agent) HostID() string {
	if c := a.creds.Load(); c != nil {
		return c.HostID
	}
	return ""
}

func (a *Agent) pkiDir() string    { return filepath.Join(a.cfg.StateDir, "pki") }
func (a *Agent) CachePath() string { return filepath.Join(a.cfg.StateDir, "cache.db") }

// Enrol loads existing credentials or performs the one-time enrolment.
func (a *Agent) Enrol(ctx context.Context) error {
	if c, err := pki.LoadHostCreds(a.pkiDir()); err == nil {
		a.creds.Store(c)
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("load host credentials: %w", err)
	}
	if a.cfg.JoinToken == "" {
		return errors.New("host is not enrolled and no join token was given (BOSS_HOSTD_JOIN_TOKEN_FILE)")
	}
	ck, sk, csr, caCSR, err := freshCSRs(a.cfg.Hostname)
	if err != nil {
		return err
	}
	labels := map[string]string{"pool": a.cfg.Pool}
	for _, l := range a.cfg.Labels {
		k, v, _ := strings.Cut(l, "=")
		labels[k] = v
	}
	resp, err := a.client.Enrol(ctx, hostapi.EnrolRequest{
		JoinToken: a.cfg.JoinToken, CSR: csr, SVIDCSR: caCSR, Hostname: a.cfg.Hostname,
		Backend: a.cfg.Manager.Name(), Facts: a.facts(ctx), Capacity: a.capacity(), Labels: labels,
		Role: hostapi.RoleHost, Address: a.cfg.Address,
	})
	if err != nil {
		return fmt.Errorf("enrol: %w", err)
	}
	c, err := a.adopt(resp, ck, sk)
	if err != nil {
		return err
	}
	if err := c.Save(a.pkiDir()); err != nil {
		return fmt.Errorf("save credentials: %w", err)
	}
	a.creds.Store(c)
	a.client.Rotate()
	if a.cfg.JoinTokenFile != "" {
		// The token is spent; do not leave it on disk.
		if err := os.Remove(a.cfg.JoinTokenFile); err != nil && !errors.Is(err, os.ErrNotExist) {
			a.logf("could not delete the spent join token file: %v", err)
		}
	}
	a.logf("enrolled as %s (vm_prefix %s)", c.HostID, c.VMPrefix)
	return nil
}

// adopt validates an enrol/renew response. The response arrived over the
// pinned TLS channel; its fleet CA is pinned at enrolment, and a renewal
// under a different fleet CA is refused. Both certificates must chain to it
// and match the keys this host generated.
func (a *Agent) adopt(resp *hostapi.EnrolResponse, ck, sk *ecdsaKey) (*pki.HostCreds, error) {
	prev := a.creds.Load()
	fleetCA := resp.CA
	if prev != nil && prev.FleetCAPEM != "" {
		if fleetCA != "" && strings.TrimSpace(fleetCA) != strings.TrimSpace(prev.FleetCAPEM) {
			return nil, errors.New("renewal answered under a different fleet CA: refused")
		}
		fleetCA = prev.FleetCAPEM
	}
	roots, err := pki.PoolFromPEM([]byte(fleetCA))
	if err != nil {
		return nil, fmt.Errorf("fleet CA: %w", err)
	}
	client, err := pki.ParseCert(resp.Certificate)
	if err != nil {
		return nil, fmt.Errorf("host certificate: %w", err)
	}
	if resp.SVIDIntermediate == "" {
		return nil, errors.New("the control plane issued no SVID intermediate")
	}
	svidCA, err := pki.ParseCert(resp.SVIDIntermediate)
	if err != nil {
		return nil, fmt.Errorf("svid intermediate: %w", err)
	}
	opts := x509.VerifyOptions{Roots: roots, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageAny}}
	if _, err := client.Verify(opts); err != nil {
		return nil, fmt.Errorf("host certificate does not chain to the fleet CA: %w", err)
	}
	if _, err := svidCA.Verify(opts); err != nil {
		return nil, fmt.Errorf("svid intermediate does not chain to the fleet CA: %w", err)
	}
	if !svidCA.IsCA {
		return nil, errors.New("svid intermediate is not a CA")
	}
	if !ck.PublicKey.Equal(client.PublicKey) || !sk.PublicKey.Equal(svidCA.PublicKey) {
		return nil, errors.New("issued certificates do not match this host's keys")
	}
	c := &pki.HostCreds{HostID: resp.HostID, TrustDomain: resp.TrustDomain, VMPrefix: resp.VMPrefix,
		ClientKey: ck, ClientCert: client, ClientPEM: resp.Certificate,
		SVIDCAKey: sk, SVIDCACert: svidCA, SVIDCAPEM: resp.SVIDIntermediate, FleetCAPEM: fleetCA}
	if prev != nil {
		if c.HostID == "" {
			c.HostID = prev.HostID
		}
		if c.TrustDomain == "" {
			c.TrustDomain = prev.TrustDomain
		}
		if c.VMPrefix == "" {
			c.VMPrefix = prev.VMPrefix
		}
	}
	if c.HostID == "" {
		return nil, errors.New("the control plane returned no host id")
	}
	return c, nil
}

// Renew replaces the client certificate and intermediate (2/3 of lifetime).
func (a *Agent) Renew(ctx context.Context) error {
	cur := a.creds.Load()
	if cur == nil {
		return errors.New("not enrolled")
	}
	ck, sk, csr, caCSR, err := freshCSRs(a.cfg.Hostname)
	if err != nil {
		return err
	}
	resp, err := a.client.Renew(ctx, cur.HostID, hostapi.RenewRequest{CSR: csr, SVIDCSR: caCSR})
	if err != nil {
		if cpclient.IsRevoked(err) {
			a.setRevoked()
		}
		return err
	}
	c, err := a.adopt(resp, ck, sk)
	if err != nil {
		return err
	}
	if err := c.Save(a.pkiDir()); err != nil {
		return err
	}
	a.creds.Store(c)
	a.client.Rotate()
	if a.gw != nil {
		a.gw.RotateHostTransport()
	}
	a.logf("renewed host certificate; next renewal at %s", pki.RenewAt(c.ClientCert).UTC().Format(time.RFC3339))
	return nil
}

func (a *Agent) setRevoked() {
	if !a.revoked.Swap(true) {
		a.logf("control plane refused this host as REVOKED: no new VMs, guest gateway closed")
	}
}

// Revoked reports whether the control plane has refused this host.
func (a *Agent) Revoked() bool { return a.revoked.Load() }

// Run enrols, adopts what is running, and runs every loop until ctx ends.
func (a *Agent) Run(ctx context.Context) error {
	if err := a.Enrol(ctx); err != nil {
		return err
	}
	c, err := cache.Open(a.CachePath())
	if err != nil {
		return fmt.Errorf("open cache: %w", err)
	}
	a.cache = c
	defer c.Close()

	a.gw = gateway.New(a.HostID(), a.minter, a.upstreamCAs())
	a.gw.Enabled = func() bool { return !a.revoked.Load() && !a.headlessExpired() }
	a.gw.CPBase = a.cpBase
	a.gw.HostProof = a.prove
	a.gw.HostCert = func() *tls.Certificate {
		if c := a.creds.Load(); c != nil {
			cert := c.TLSCertificate()
			return &cert
		}
		return nil
	}
	a.calls = &gateway.Batcher[hostapi.GuestCall]{MaxBatch: 200, Cap: 10000,
		Flush: func(ctx context.Context, rows []hostapi.GuestCall) error {
			return a.client.PostGuestCalls(ctx, a.HostID(), rows)
		}}
	a.events = &gateway.Batcher[hostapi.HostEvent]{MaxBatch: 200, Cap: 10000, Every: 500 * time.Millisecond,
		Flush: func(ctx context.Context, rows []hostapi.HostEvent) error {
			if a.noEvents.Load() {
				return nil // no events endpoint: state still flows via heartbeat + observed
			}
			err := a.client.PostEvents(ctx, a.HostID(), rows)
			if errors.Is(err, cpclient.ErrNotSupported) {
				a.noEvents.Store(true)
				a.logf("control plane has no /events endpoint: watchdog events go via heartbeat and observed only")
				return nil
			}
			return err
		}}
	a.gw.Audit = a.calls.Add
	a.gw.Event = a.events.Add

	a.touchCP() // the headless window starts now if the control plane never answers
	if err := a.adoptRunning(ctx); err != nil {
		a.logf("adopt scan: %v", err)
	}
	if ds, err := c.LastDesired(); err == nil && ds != nil {
		// Headless start: the cached state has no handles, so the gateway
		// refuses calls until the control plane answers; VMs keep running.
		a.mu.Lock()
		if a.desired == nil {
			a.desired = ds
		}
		a.mu.Unlock()
	}

	var wg sync.WaitGroup
	for _, loop := range []func(context.Context){a.watchLoop, a.heartbeatLoop, a.renewLoop, a.reconcileLoop,
		a.observedLoop, a.workLoop, a.calls.Run, a.events.Run} {
		wg.Add(1)
		go func(f func(context.Context)) { defer wg.Done(); f(ctx) }(loop)
	}
	wg.Wait()
	a.shutdownGateway()
	return nil
}

func (a *Agent) renewLoop(ctx context.Context) {
	for {
		c := a.creds.Load()
		wait := time.Until(pki.RenewAt(c.ClientCert))
		if wait < time.Second {
			wait = time.Second // never a hot loop, even on a bad clock
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		case <-a.renewNow: // the control plane asked (heartbeat "renew": true)
		}
		if err := a.Renew(ctx); err != nil {
			a.logf("renew failed: %v (retrying in 30s)", err)
			select {
			case <-ctx.Done():
				return
			case <-time.After(30 * time.Second):
			}
		}
	}
}

func (a *Agent) touchCP() { a.lastCP.Store(time.Now().UnixNano()) }

// headlessExpired reports whether the control plane has been unreachable for
// longer than HeadlessMax.
func (a *Agent) headlessExpired() bool {
	return time.Since(time.Unix(0, a.lastCP.Load())) > a.cfg.HeadlessMax
}

// bumpState records a replica state change: a new generation, pushed to
// /observed at once (debounced 100 ms) with the generation it reflects.
func (a *Agent) bumpState() {
	a.stateGen.Add(1)
	select {
	case a.pushObs <- struct{}{}:
	default:
	}
}

// StateGen is the current replica-state generation.
func (a *Agent) StateGen() int64 { return a.stateGen.Load() }

// detailOnly are event kinds that change no replica state: they neither
// bump state_gen nor push /observed.
var detailOnly = map[string]bool{"handed_back": true, "handle_replay": true, "orphan_tap_removed": true, "director_push": true,
	"vm_timing": true, "warm_ready": true, "warm_failed": true, "warm_dropped": true, "snapshot": true,
	"snapshot_failed": true, "snapshot_gc": true}

func (a *Agent) event(vmID, kind, from, to string, detail map[string]string) {
	if !detailOnly[kind] {
		a.bumpState()
	}
	if a.events == nil {
		return
	}
	a.events.Add(hostapi.HostEvent{At: hostapi.Now(), VMID: vmID, Kind: kind,
		From: from, To: to, Detail: detail, Actor: "watchdog"})
}

// upstreamCAs trusts the pinned CP bundle and the fleet CA (central
// gateways present fleet-CA certificates; the run plane is the CP itself).
func (a *Agent) upstreamCAs() *x509.CertPool {
	pool := x509.NewCertPool()
	if len(a.cfg.PinnedCA) > 0 {
		pool.AppendCertsFromPEM(a.cfg.PinnedCA)
	}
	if c := a.creds.Load(); c != nil && c.FleetCAPEM != "" {
		pool.AppendCertsFromPEM([]byte(c.FleetCAPEM))
	}
	return pool
}
