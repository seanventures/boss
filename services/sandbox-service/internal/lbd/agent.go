package lbd

import (
	"context"
	"crypto/ecdsa"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/cpclient"
	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanheiney/boss/services/sandbox-service/internal/lbd/ipvs"
	"github.com/seanheiney/boss/services/sandbox-service/internal/pki"
)

// Config is everything boss-lbd needs. Zero values take the defaults.
type Config struct {
	StateDir string // pki/ (default /var/lib/boss-lbd)
	RunDir   string // state/ and track/ (default /run/boss-lbd)

	// Control plane (production). Empty CPURL with DesiredFile set is lab
	// mode: desired state from a file, no enrolment, certificates from files.
	CPURL         string
	PinnedCA      []byte
	JoinToken     string
	JoinTokenFile string

	DesiredFile string
	LeaseFile   string // lab: shared lease file (the lease rules without a control plane)
	ID          string // lab: this director's id (default: the certificate's CN)
	CertFile    string // lab: this director's certificate
	KeyFile     string // lab
	CAFile      string // lab: the fleet CA

	Address     string // this director's IPv4 (GRE source, push listener, VRRP unicast)
	Iface       string // VRRP + IPVS sync interface (default: the one holding Address)
	PushPort    int    // 7446
	MetricsAddr string // 127.0.0.1:9446

	KeepalivedConf string // /etc/keepalived/keepalived.conf
	KeepalivedPid  string // /run/keepalived/keepalived.pid
	SelfBin        string // this binary's path (track/notify scripts)
	Hostname       string

	NoKeepalived bool
	NoSync       bool
	NoRoutes     bool
	NoSysctl     bool
	NoHealth     bool

	Backend ipvs.Backend // nil = the kernel
	Prober  Prober       // nil = GREProber

	HeartbeatEvery time.Duration // 0.5 s (the director lease's renewal; TTL 2 s)
	WatchWait      time.Duration // 25 s
	ReconcileEvery time.Duration // 10 s
	Logf           func(string, ...any)
}

func (c *Config) defaults() {
	if c.StateDir == "" {
		c.StateDir = "/var/lib/boss-lbd"
	}
	if c.RunDir == "" {
		c.RunDir = "/run/boss-lbd"
	}
	if c.PushPort <= 0 {
		c.PushPort = DefaultPushPort
	}
	if c.MetricsAddr == "" {
		c.MetricsAddr = "127.0.0.1:9446"
	}
	if c.KeepalivedConf == "" {
		c.KeepalivedConf = "/etc/keepalived/keepalived.conf"
	}
	if c.KeepalivedPid == "" {
		c.KeepalivedPid = "/run/keepalived/keepalived.pid"
	}
	if c.SelfBin == "" {
		if exe, err := os.Executable(); err == nil {
			c.SelfBin = exe
		} else {
			c.SelfBin = "boss-lbd"
		}
	}
	if c.HeartbeatEvery <= 0 {
		c.HeartbeatEvery = 500 * time.Millisecond
	}
	if c.WatchWait <= 0 {
		c.WatchWait = 25 * time.Second
	}
	if c.ReconcileEvery <= 0 {
		c.ReconcileEvery = 10 * time.Second
	}
	if c.Logf == nil {
		c.Logf = log.Printf
	}
}

// Agent is one running director.
type Agent struct {
	cfg     Config
	creds   atomic.Pointer[Creds]
	client  *cpclient.Client
	prog    *Programmer
	checker *Checker
	metrics *Metrics
	routes  Routes
	be      ipvs.Backend

	mu       sync.Mutex
	desired  *hostapi.DirectorDesired
	leases   map[string]string
	tracker  LeaseTracker
	lastCP   atomic.Int64
	kick     chan struct{}
	renewNow chan struct{}
}

// New prepares an agent.
func New(cfg Config) (*Agent, error) {
	cfg.defaults()
	if cfg.CPURL == "" && cfg.DesiredFile == "" {
		return nil, errors.New("boss-lbd: BOSS_LBD_CP_URL (or a lab --desired-file) is required")
	}
	if net.ParseIP(cfg.Address).To4() == nil {
		return nil, errors.New("boss-lbd: BOSS_LBD_ADDRESS (this director's IPv4) is required")
	}
	a := &Agent{cfg: cfg, metrics: NewMetrics(), kick: make(chan struct{}, 1), renewNow: make(chan struct{}, 1),
		leases: map[string]string{}}
	a.be = cfg.Backend
	if a.be == nil {
		be, err := ipvs.Open()
		if err != nil {
			return nil, err
		}
		a.be = be
	}
	a.prog = NewProgrammer(a.be, func(f string, args ...any) { a.logf(f, args...) })
	prober := cfg.Prober
	if prober == nil && !cfg.NoHealth {
		prober = &GREProber{Local: net.ParseIP(cfg.Address).To4()}
	}
	a.checker = &Checker{Prober: prober, Prog: a.prog, Metrics: a.metrics, Logf: func(f string, args ...any) { a.logf(f, args...) }}
	if cfg.CPURL != "" {
		var pool *x509.CertPool
		if len(cfg.PinnedCA) > 0 {
			var err error
			if pool, err = pki.PoolFromPEM(cfg.PinnedCA); err != nil {
				return nil, fmt.Errorf("pinned control-plane CA: %w", err)
			}
		}
		client, err := cpclient.New(cfg.CPURL, pool, func() *tls.Certificate {
			if c := a.creds.Load(); c != nil {
				cert := c.TLSCertificate()
				return &cert
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
		client.Prove = func(method, path string) (string, error) {
			c := a.creds.Load()
			if c == nil {
				return "", errors.New("not enrolled")
			}
			return pki.SignHostProof(c.Key, c.Cert, method, path)
		}
		a.client = client
	}
	return a, nil
}

func (a *Agent) logf(format string, args ...any) { a.cfg.Logf("boss-lbd: "+format, args...) }

// ID is this director's id ("" before enrolment).
func (a *Agent) ID() string {
	if c := a.creds.Load(); c != nil {
		return c.ID
	}
	return a.cfg.ID
}

// Metrics exposes the exporter (tests).
func (a *Agent) Metrics() *Metrics { return a.metrics }

// Programmer exposes the IPVS programmer (tests).
func (a *Agent) Programmer() *Programmer { return a.prog }

func (a *Agent) pkiDir() string { return filepath.Join(a.cfg.StateDir, "pki") }

// Enrol loads credentials (files in lab mode, the pki dir otherwise) or
// spends the join token once.
func (a *Agent) Enrol(ctx context.Context) error {
	if a.cfg.CPURL == "" {
		c, err := LoadFiles(a.cfg.ID, a.cfg.KeyFile, a.cfg.CertFile, a.cfg.CAFile)
		if err != nil {
			return fmt.Errorf("lab credentials: %w", err)
		}
		a.creds.Store(c)
		return nil
	}
	if c, err := LoadCreds(a.pkiDir()); err == nil {
		a.creds.Store(c)
		a.client.Rotate()
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("load director credentials: %w", err)
	}
	if a.cfg.JoinToken == "" {
		return errors.New("director is not enrolled and no join token was given (BOSS_LBD_JOIN_TOKEN_FILE)")
	}
	key, err := pki.GenerateKey()
	if err != nil {
		return err
	}
	csr, err := pki.CSR(key, a.cfg.Hostname)
	if err != nil {
		return err
	}
	resp, err := a.client.Enrol(ctx, hostapi.EnrolRequest{JoinToken: a.cfg.JoinToken, CSR: csr, Hostname: a.cfg.Hostname,
		Backend: "process", Role: hostapi.RoleDirector, Address: a.cfg.Address, Labels: map[string]string{"role": "director"}})
	if err != nil {
		return fmt.Errorf("enrol: %w", err)
	}
	c, err := adopt(resp, key, "")
	if err != nil {
		return err
	}
	if err := c.Save(a.pkiDir()); err != nil {
		return err
	}
	a.creds.Store(c)
	a.client.Rotate()
	if a.cfg.JoinTokenFile != "" {
		if err := os.Remove(a.cfg.JoinTokenFile); err != nil && !errors.Is(err, os.ErrNotExist) {
			a.logf("could not delete the spent join token file: %v", err)
		}
	}
	a.logf("enrolled as director %s", c.ID)
	return nil
}

// adopt validates an enrol/renew answer: the certificate chains to the
// fleet CA (pinned from the first enrolment) and matches our key.
func adopt(resp *hostapi.EnrolResponse, key *ecdsa.PrivateKey, pinnedFleet string) (*Creds, error) {
	fleet := resp.CA
	if pinnedFleet != "" {
		if fleet != "" && strings.TrimSpace(fleet) != strings.TrimSpace(pinnedFleet) {
			return nil, errors.New("renewal answered under a different fleet CA: refused")
		}
		fleet = pinnedFleet
	}
	roots, err := pki.PoolFromPEM([]byte(fleet))
	if err != nil {
		return nil, fmt.Errorf("fleet CA: %w", err)
	}
	cert, err := pki.ParseCert(resp.Certificate)
	if err != nil {
		return nil, fmt.Errorf("director certificate: %w", err)
	}
	if _, err := cert.Verify(x509.VerifyOptions{Roots: roots, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageAny}}); err != nil {
		return nil, fmt.Errorf("director certificate does not chain to the fleet CA: %w", err)
	}
	if !key.PublicKey.Equal(cert.PublicKey) {
		return nil, errors.New("issued certificate does not match this director's key")
	}
	if resp.HostID == "" {
		return nil, errors.New("the control plane returned no director id")
	}
	return &Creds{ID: resp.HostID, TrustDomain: resp.TrustDomain, Key: key, Cert: cert, CertPEM: resp.Certificate,
		FleetCAPEM: fleet}, nil
}

// Run enrols and runs every loop until ctx ends.
func (a *Agent) Run(ctx context.Context) error {
	if err := a.Enrol(ctx); err != nil {
		return err
	}
	if err := os.MkdirAll(a.cfg.RunDir, 0o755); err != nil {
		return err
	}
	if a.cfg.Iface == "" {
		a.cfg.Iface = DefaultIface(a.cfg.Address)
	}
	if !a.cfg.NoSysctl {
		if err := Sysctls(a.cfg.Iface); err != nil {
			a.logf("sysctl: %v", err)
		}
	}
	if !a.cfg.NoSync && a.cfg.Iface != "" {
		for _, st := range []uint32{ipvs.DaemonMaster, ipvs.DaemonBackup} {
			if err := a.be.StartDaemon(st, a.cfg.Iface, 0); err != nil {
				a.logf("ipvs sync daemon %d on %s: %v", st, a.cfg.Iface, err)
			}
		}
	}
	a.touchCP()
	pushSrv, err := a.pushServer()
	if err != nil {
		return err
	}
	metricsSrv := &http.Server{Addr: a.cfg.MetricsAddr, Handler: a.metricsHandler(), ReadHeaderTimeout: 5 * time.Second}
	go func() {
		if err := metricsSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			a.logf("metrics listener: %v", err)
		}
	}()
	go func() { <-ctx.Done(); _ = metricsSrv.Close(); _ = pushSrv.Close() }()

	var wg sync.WaitGroup
	loops := []func(context.Context){a.watchLoop, a.reconcileLoop, a.healthLoop}
	if a.client != nil {
		loops = append(loops, a.heartbeatLoop, a.renewLoop)
	} else {
		loops = append(loops, a.labLeaseLoop)
	}
	for _, loop := range loops {
		wg.Add(1)
		go func(f func(context.Context)) { defer wg.Done(); f(ctx) }(loop)
	}
	wg.Wait()
	if !a.cfg.NoSync {
		_ = a.be.StopDaemon(ipvs.DaemonMaster)
		_ = a.be.StopDaemon(ipvs.DaemonBackup)
	}
	a.be.Close()
	return nil
}

func (a *Agent) touchCP() { a.lastCP.Store(time.Now().UnixNano()) }

func (a *Agent) pushServer() (*http.Server, error) {
	c := a.creds.Load()
	pool, err := c.FleetPool()
	if err != nil {
		return nil, fmt.Errorf("fleet CA: %w", err)
	}
	ps := &PushServer{Prog: a.prog, Metrics: a.metrics, Logf: func(f string, args ...any) { a.logf(f, args...) }}
	srv := &http.Server{Addr: net.JoinHostPort(a.cfg.Address, strconv.Itoa(a.cfg.PushPort)), Handler: ps.Handler(),
		ReadHeaderTimeout: 2 * time.Second, ReadTimeout: 5 * time.Second, WriteTimeout: 5 * time.Second,
		TLSConfig: ServerTLS(func() *tls.Certificate {
			if c := a.creds.Load(); c != nil {
				cert := c.TLSCertificate()
				return &cert
			}
			return nil
		}, pool)}
	ln, err := net.Listen("tcp", srv.Addr)
	if err != nil {
		return nil, fmt.Errorf("push listener %s: %w", srv.Addr, err)
	}
	go func() {
		if err := srv.ServeTLS(ln, "", ""); err != nil && !errors.Is(err, http.ErrServerClosed) {
			a.logf("push listener: %v", err)
		}
	}()
	a.logf("withdraw-push listener on %s (mTLS, fleet CA)", srv.Addr)
	return srv, nil
}

func (a *Agent) metricsHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, _ *http.Request) {
		a.refreshGauges()
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		a.metrics.Write(w)
	})
	mux.HandleFunc("/status", func(w http.ResponseWriter, _ *http.Request) {
		a.mu.Lock()
		d := a.desired
		leases := map[string]string{}
		for k, v := range a.leases {
			leases[k] = v
		}
		a.mu.Unlock()
		svc, rs := a.prog.Counts()
		out := map[string]any{"director_id": a.ID(), "address": a.cfg.Address, "iface": a.cfg.Iface,
			"services": svc, "real_servers": rs, "overrides": a.prog.Overrides(), "leases": leases,
			"keepalived": ReadStates(a.cfg.RunDir), "health_down": a.checker.Down()}
		if d != nil {
			out["generation"] = d.Generation
			out["vips"] = len(d.VIPs)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(out)
	})
	return mux
}

func (a *Agent) refreshGauges() {
	svc, rs := a.prog.Counts()
	a.metrics.Set("boss_lbd_services", float64(svc))
	a.metrics.Set("boss_lbd_real_servers", float64(rs))
	a.metrics.Set("boss_lbd_overrides", float64(a.prog.Overrides()))
	if st, err := a.be.Stats(); err == nil {
		a.metrics.Set("boss_lbd_ipvs_conns_total", float64(st.Conns))
		a.metrics.Set("boss_lbd_ipvs_cps", float64(st.CPS))
		a.metrics.Set("boss_lbd_ipvs_pps_in", float64(st.PPSIn))
		a.metrics.Set("boss_lbd_ipvs_bps_in", float64(st.BPSIn))
	}
	if n, sz, err := ipvs.ConnMemory(); err == nil {
		a.metrics.Set("boss_lbd_ipvs_conn_entries", float64(n))
		a.metrics.Set("boss_lbd_ipvs_conn_bytes_each", float64(sz))
	}
}

// ---- desired state --------------------------------------------------------

func (a *Agent) watchLoop(ctx context.Context) {
	var since int64
	backoff := time.Second
	for ctx.Err() == nil {
		var ds *hostapi.DirectorDesired
		var err error
		if a.client == nil {
			if since != 0 { // the first read applies at once; later ones poll the file
				select {
				case <-ctx.Done():
					return
				case <-time.After(2 * time.Second):
				}
			}
			ds, err = a.readDesiredFile()
			if err == nil && ds != nil && ds.Generation == since {
				ds = nil
			}
		} else {
			ds, err = a.client.DirectorDesired(ctx, a.ID(), since, a.cfg.WatchWait)
		}
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			a.logf("desired: %v", err)
			since = 0
			select {
			case <-ctx.Done():
				return
			case <-time.After(backoff):
			}
			if backoff < 10*time.Second {
				backoff *= 2
			}
			continue
		}
		backoff = time.Second
		a.touchCP()
		if ds == nil {
			continue
		}
		a.applyDesired(ds)
		since = ds.Generation
	}
}

func (a *Agent) readDesiredFile() (*hostapi.DirectorDesired, error) {
	raw, err := os.ReadFile(a.cfg.DesiredFile)
	if err != nil {
		return nil, err
	}
	var ds hostapi.DirectorDesired
	if err := json.Unmarshal(raw, &ds); err != nil {
		return nil, fmt.Errorf("desired file: %w", err)
	}
	if ds.Generation == 0 {
		if st, err := os.Stat(a.cfg.DesiredFile); err == nil {
			ds.Generation = st.ModTime().UnixNano()
		}
	}
	ds.Changed = true
	if ds.DirectorID == "" {
		ds.DirectorID = a.ID()
	}
	return &ds, nil
}

func (a *Agent) applyDesired(ds *hostapi.DirectorDesired) {
	if len(ds.Groups) == 0 && len(ds.VIPs) > 0 {
		var vips, ids []string
		for _, v := range ds.VIPs {
			vips = append(vips, v.VIP)
		}
		for _, d := range ds.Directors {
			ids = append(ids, d.ID)
		}
		if len(ids) == 0 {
			ids = []string{a.ID()}
		}
		ds.Groups = Group(vips, ids)
	}
	a.mu.Lock()
	a.desired = ds
	a.mu.Unlock()
	a.prog.SetDesired(ds)
	a.metrics.Set("boss_lbd_desired_generation", float64(ds.Generation))
	a.applyNow("desired")
	if !a.cfg.NoRoutes {
		if err := a.routes.Apply(ds.Hosts); err != nil {
			a.logf("routes: %v", err)
		}
	}
	if !a.cfg.NoKeepalived {
		a.writeKeepalived(ds)
	}
}

func (a *Agent) applyNow(why string) {
	start := time.Now()
	ch, err := a.prog.Apply()
	a.metrics.Inc("boss_lbd_apply_total", 1)
	if err != nil || ch.Errors > 0 {
		a.metrics.Inc("boss_lbd_apply_errors_total", 1)
		a.logf("apply (%s): %s err=%v", why, ch, err)
		return
	}
	if ch.ServicesAdded+ch.ServicesUpdated+ch.ServicesRemoved+ch.DestsAdded+ch.DestsUpdated+ch.DestsRemoved > 0 {
		a.logf("apply (%s): %s in %s", why, ch, time.Since(start).Round(time.Microsecond))
	}
}

func (a *Agent) writeKeepalived(ds *hostapi.DirectorDesired) {
	self := a.cfg.Address
	var peer string
	if ds.Peer != nil {
		peer = ds.Peer.Address
	}
	conf := RenderKeepalived(KeepalivedConfig{SelfID: a.ID(), SelfAddress: self, PeerAddress: peer, Iface: a.cfg.Iface,
		Groups: ds.Groups, TrackBin: a.cfg.SelfBin, StateDir: a.cfg.RunDir})
	changed, err := WriteIfChanged(a.cfg.KeepalivedConf, conf)
	if err != nil {
		a.logf("keepalived config: %v", err)
		return
	}
	if changed {
		a.logf("keepalived config written: %d VRRP groups (%d VIPs); reloading", len(ds.Groups), countVIPs(ds.Groups))
		if err := ReloadKeepalived(a.cfg.KeepalivedPid); err != nil {
			a.logf("keepalived reload: %v", err)
		}
	}
}

func countVIPs(gs []hostapi.VRRPGroup) int {
	n := 0
	for _, g := range gs {
		n += len(g.VIPs)
	}
	return n
}

func (a *Agent) reconcileLoop(ctx context.Context) {
	t := time.NewTicker(a.cfg.ReconcileEvery)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			a.applyNow("periodic")
		case <-a.kick:
			a.applyNow("kick")
		}
	}
}

func (a *Agent) healthLoop(ctx context.Context) {
	if a.cfg.NoHealth || a.checker.Prober == nil {
		return
	}
	t := time.NewTicker(CheckInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			a.checker.Tick(ctx)
		}
	}
}

// ---- heartbeat and lease ---------------------------------------------------

func (a *Agent) heartbeatLoop(ctx context.Context) {
	t := time.NewTicker(a.cfg.HeartbeatEvery)
	defer t.Stop()
	for {
		a.heartbeatOnce(ctx)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

func (a *Agent) heartbeatOnce(ctx context.Context) {
	masters := a.masters()
	svc, rs := a.prog.Counts()
	var conns int64
	if st, err := a.be.Stats(); err == nil {
		conns = int64(st.Conns)
	}
	sync := "master+backup"
	if a.cfg.NoSync {
		sync = "off"
	}
	var gen int64
	a.mu.Lock()
	if a.desired != nil {
		gen = a.desired.Generation
	}
	a.mu.Unlock()
	states := a.states()
	sentAt := time.Now()
	hctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	resp, err := a.client.DirectorHeartbeat(hctx, a.ID(), hostapi.DirectorHeartbeatRequest{Address: a.cfg.Address,
		GroupsMaster: masters, KeepalivedLive: KeepalivedLive(a.cfg.KeepalivedPid),
		IPVS:               hostapi.DirectorIPVSState{Services: svc, Dests: rs, Conns: conns, SyncState: sync},
		ObservedGeneration: gen, ContractVersion: hostapi.Version})
	if err != nil {
		a.logf("heartbeat: %v", err)
		a.metrics.Inc("boss_lbd_heartbeat_errors_total", 1)
		return // an unreachable control plane never faults a group (decision 30)
	}
	a.touchCP()
	leases := map[string]LeaseInfo{}
	for g, h := range resp.Leases {
		leases[g] = LeaseInfo{Holder: h, Expires: resp.LeaseExpires[g]}
	}
	a.applyLeases(sentAt, states, leases)
	if resp.Renew {
		select {
		case a.renewNow <- struct{}{}:
		default:
		}
	}
}

// masters are the groups keepalived reports MASTER — none when keepalived is
// not running (a SIGKILLed keepalived never writes STOP; its stale MASTER
// files must not keep renewing leases the peer needs).
func (a *Agent) masters() []int { return Masters(a.states()) }

func (a *Agent) states() map[int]string {
	if !a.cfg.NoKeepalived && !KeepalivedLive(a.cfg.KeepalivedPid) {
		return map[int]string{}
	}
	return ReadStates(a.cfg.RunDir)
}

// applyLeases runs the lease tracker and writes the track files.
func (a *Agent) applyLeases(sentAt time.Time, states map[int]string, leases map[string]LeaseInfo) {
	holders := map[string]string{}
	for g, l := range leases {
		holders[g] = l.Holder
	}
	a.mu.Lock()
	a.leases = holders
	a.tracker.Self = a.ID()
	a.mu.Unlock()
	faults := a.tracker.Observe(sentAt, a.groupIDs(), states, leases)
	n := 0
	for _, f := range faults {
		if f {
			n++
		}
	}
	a.metrics.Set("boss_lbd_lease_faults", float64(n))
	if err := WriteTrack(a.cfg.RunDir, faults); err != nil {
		a.logf("track files: %v", err)
	}
	if n > 0 {
		a.logf("lease: split brain on %d group(s): another live director renewed the lease after we became MASTER → FAULT", n)
	}
}

// groupIDs are the VRRP groups of the current desired state.
func (a *Agent) groupIDs() []int {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.desired == nil {
		return nil
	}
	out := make([]int, 0, len(a.desired.Groups))
	for _, g := range a.desired.Groups {
		out = append(out, g.Group)
	}
	return out
}

// labLeaseLoop (no control plane): with a lease file (--lease-file) the
// same lease rules run against a shared file, so the lab exercises the
// lease and split-brain path; without one every group is "ok" and
// keepalived decides alone.
func (a *Agent) labLeaseLoop(ctx context.Context) {
	t := time.NewTicker(a.cfg.HeartbeatEvery)
	defer t.Stop()
	for {
		states := a.states()
		sentAt := time.Now()
		var leases map[string]LeaseInfo
		if a.cfg.LeaseFile != "" {
			l, err := FileLeases{Path: a.cfg.LeaseFile}.Acquire(a.ID(), Masters(states))
			if err != nil {
				a.logf("lease file: %v", err)
			}
			leases = l
		}
		a.applyLeases(sentAt, states, leases)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

func (a *Agent) renewLoop(ctx context.Context) {
	for {
		c := a.creds.Load()
		wait := time.Until(pki.RenewAt(c.Cert))
		if wait < time.Second {
			wait = time.Second
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		case <-a.renewNow:
		}
		if err := a.renew(ctx); err != nil {
			a.logf("renew failed: %v (retrying in 30s)", err)
			select {
			case <-ctx.Done():
				return
			case <-time.After(30 * time.Second):
			}
		}
	}
}

func (a *Agent) renew(ctx context.Context) error {
	cur := a.creds.Load()
	key, err := pki.GenerateKey()
	if err != nil {
		return err
	}
	csr, err := pki.CSR(key, a.cfg.Hostname)
	if err != nil {
		return err
	}
	resp, err := a.client.DirectorRenew(ctx, cur.ID, hostapi.RenewRequest{CSR: csr})
	if err != nil {
		return err
	}
	c, err := adopt(resp, key, cur.FleetCAPEM)
	if err != nil {
		return err
	}
	if c.ID == "" {
		c.ID = cur.ID
	}
	if err := c.Save(a.pkiDir()); err != nil {
		return err
	}
	a.creds.Store(c)
	a.client.Rotate()
	a.logf("renewed director certificate; next renewal at %s", pki.RenewAt(c.Cert).UTC().Format(time.RFC3339))
	return nil
}
