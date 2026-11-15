// boss-lbd is the IPVS director (LLD §3.14.2, P8b-10, BOSSAPP-1077). It
// programs the kernel's IP Virtual Server from BOSS desired state (GRE tunnel
// real servers, rr/wlc, persistence off), renders keepalived's VRRP-only
// configuration, runs the IPVS connection sync daemons, holds the director
// lease through the control plane, health-checks every real server through
// the real data path, and accepts sub-second withdraw pushes from hosts over
// mTLS. Routing never decides ownership: a director fault resets connections
// at worst (LLD §2.8).
//
//	boss-lbd                              run (env below)
//	boss-lbd track --group N              keepalived vrrp_script (lease)
//	boss-lbd notify --group N --to STATE  keepalived notify_* script
//	boss-lbd push ...                     send one member push (diagnostics, labs)
//	boss-lbd render-keepalived --desired-file F --id ID --address A --iface I
//
// Environment (docs/infra/HOST_API.md §9):
//
//	BOSS_LBD_CP_URL              control-plane base URL (https); unset + BOSS_LBD_DESIRED_FILE = lab mode
//	BOSS_LBD_CP_CA               PEM file: the pinned control-plane CA
//	BOSS_LBD_JOIN_TOKEN_FILE     one-time join token (role director; first start only)
//	BOSS_LBD_STATE_DIR           pki/              (default /var/lib/boss-lbd)
//	BOSS_LBD_RUN_DIR             state/, track/    (default /run/boss-lbd)
//	BOSS_LBD_ADDRESS             this director's IPv4 (required)
//	BOSS_LBD_IFACE               VRRP + sync interface (default: the one holding the address)
//	BOSS_LBD_PUSH_PORT           mTLS withdraw-push listener (default 7446)
//	BOSS_LBD_METRICS_ADDR        Prometheus text + /status (default 127.0.0.1:9446)
//	BOSS_LBD_KEEPALIVED_CONF     (default /etc/keepalived/keepalived.conf)
//	BOSS_LBD_KEEPALIVED_PID      (default /run/keepalived/keepalived.pid)
//	BOSS_LBD_DESIRED_FILE        lab: desired state JSON (hostapi.DirectorDesired)
//	BOSS_LBD_ID, BOSS_LBD_CERT, BOSS_LBD_KEY, BOSS_LBD_CA   lab: identity from files
//	BOSS_LBD_NO_KEEPALIVED, BOSS_LBD_NO_SYNC, BOSS_LBD_NO_ROUTES, BOSS_LBD_NO_SYSCTL, BOSS_LBD_NO_HEALTH
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanheiney/boss/services/sandbox-service/internal/lbd"
	"github.com/seanheiney/boss/services/sandbox-service/internal/pki"
)

func env(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

func envBool(key string, def bool) bool {
	v := strings.ToLower(env(key, ""))
	if v == "" {
		return def
	}
	return v == "1" || v == "true" || v == "yes"
}

func main() {
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "track":
			os.Exit(track(os.Args[2:]))
		case "notify":
			os.Exit(notify(os.Args[2:]))
		case "push":
			os.Exit(push(os.Args[2:]))
		case "render-keepalived":
			os.Exit(renderKeepalived(os.Args[2:]))
		}
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	cfg, err := buildConfig(os.Args[1:])
	if err != nil {
		log.Fatalf("boss-lbd: %v", err)
	}
	agent, err := lbd.New(cfg)
	if err != nil {
		log.Fatalf("boss-lbd: %v", err)
	}
	if err := agent.Run(ctx); err != nil {
		log.Fatalf("boss-lbd: %v", err)
	}
}

func buildConfig(args []string) (lbd.Config, error) {
	fs := flag.NewFlagSet("boss-lbd", flag.ExitOnError)
	cfg := lbd.Config{}
	fs.StringVar(&cfg.CPURL, "cp-url", env("BOSS_LBD_CP_URL", ""), "control-plane base URL")
	caPath := fs.String("cp-ca", env("BOSS_LBD_CP_CA", ""), "pinned control-plane CA (PEM file)")
	joinFile := fs.String("join-token-file", env("BOSS_LBD_JOIN_TOKEN_FILE", ""), "one-time join token file")
	fs.StringVar(&cfg.StateDir, "state-dir", env("BOSS_LBD_STATE_DIR", "/var/lib/boss-lbd"), "pki dir")
	fs.StringVar(&cfg.RunDir, "run-dir", env("BOSS_LBD_RUN_DIR", "/run/boss-lbd"), "state/track dir")
	fs.StringVar(&cfg.Address, "address", env("BOSS_LBD_ADDRESS", ""), "this director's IPv4")
	fs.StringVar(&cfg.Iface, "iface", env("BOSS_LBD_IFACE", ""), "VRRP and sync interface")
	port := fs.Int("push-port", atoi(env("BOSS_LBD_PUSH_PORT", "7446"), 7446), "withdraw-push mTLS port")
	fs.StringVar(&cfg.MetricsAddr, "metrics-addr", env("BOSS_LBD_METRICS_ADDR", "127.0.0.1:9446"), "metrics listener")
	fs.StringVar(&cfg.KeepalivedConf, "keepalived-conf", env("BOSS_LBD_KEEPALIVED_CONF", "/etc/keepalived/keepalived.conf"), "keepalived config path")
	fs.StringVar(&cfg.KeepalivedPid, "keepalived-pid", env("BOSS_LBD_KEEPALIVED_PID", "/run/keepalived/keepalived.pid"), "keepalived pid file")
	fs.StringVar(&cfg.DesiredFile, "desired-file", env("BOSS_LBD_DESIRED_FILE", ""), "lab: desired state JSON")
	fs.StringVar(&cfg.LeaseFile, "lease-file", env("BOSS_LBD_LEASE_FILE", ""), "lab: shared director-lease file")
	fs.StringVar(&cfg.ID, "id", env("BOSS_LBD_ID", ""), "lab: director id")
	fs.StringVar(&cfg.CertFile, "cert", env("BOSS_LBD_CERT", ""), "lab: certificate PEM")
	fs.StringVar(&cfg.KeyFile, "key", env("BOSS_LBD_KEY", ""), "lab: key PEM")
	fs.StringVar(&cfg.CAFile, "ca", env("BOSS_LBD_CA", ""), "lab: fleet CA PEM")
	fs.BoolVar(&cfg.NoKeepalived, "no-keepalived", envBool("BOSS_LBD_NO_KEEPALIVED", false), "do not write/reload keepalived")
	fs.BoolVar(&cfg.NoSync, "no-sync", envBool("BOSS_LBD_NO_SYNC", false), "no IPVS sync daemons")
	fs.BoolVar(&cfg.NoRoutes, "no-routes", envBool("BOSS_LBD_NO_ROUTES", false), "no host prefix routes")
	fs.BoolVar(&cfg.NoSysctl, "no-sysctl", envBool("BOSS_LBD_NO_SYSCTL", false), "leave sysctls alone")
	fs.BoolVar(&cfg.NoHealth, "no-health", envBool("BOSS_LBD_NO_HEALTH", false), "no health checks")
	_ = fs.Parse(args)
	cfg.PushPort = *port
	if *caPath != "" {
		ca, err := os.ReadFile(*caPath)
		if err != nil {
			return cfg, fmt.Errorf("read pinned CA: %w", err)
		}
		cfg.PinnedCA = ca
	}
	if *joinFile != "" {
		raw, err := os.ReadFile(*joinFile)
		if err != nil && !os.IsNotExist(err) {
			return cfg, fmt.Errorf("read join token file: %w", err)
		}
		cfg.JoinToken = strings.TrimSpace(string(raw))
		cfg.JoinTokenFile = *joinFile
	}
	cfg.Hostname, _ = os.Hostname()
	return cfg, nil
}

func atoi(s string, def int) int {
	n, err := strconv.Atoi(s)
	if err != nil {
		return def
	}
	return n
}

func track(args []string) int {
	fs := flag.NewFlagSet("track", flag.ExitOnError)
	group := fs.Int("group", -1, "VRRP group")
	dir := fs.String("state-dir", env("BOSS_LBD_RUN_DIR", "/run/boss-lbd"), "run dir")
	_ = fs.Parse(args)
	if *group < 0 {
		return 2
	}
	return lbd.Track(*dir, *group)
}

func notify(args []string) int {
	fs := flag.NewFlagSet("notify", flag.ExitOnError)
	group := fs.Int("group", -1, "VRRP group")
	dir := fs.String("state-dir", env("BOSS_LBD_RUN_DIR", "/run/boss-lbd"), "run dir")
	to := fs.String("to", "", "MASTER|BACKUP|FAULT|STOP")
	_ = fs.Parse(args)
	if *group < 0 || *to == "" {
		return 2
	}
	if err := lbd.WriteNotify(*dir, *group, *to); err != nil {
		fmt.Fprintln(os.Stderr, "notify:", err)
		return 1
	}
	return 0
}

// push sends one member call as a host would (diagnostics and the lab).
func push(args []string) int {
	fs := flag.NewFlagSet("push", flag.ExitOnError)
	to := fs.String("to", "", "director address[:port] (repeatable with commas)")
	ids := fs.String("director-id", "", "expected director id(s), comma-separated (optional)")
	cert := fs.String("cert", "", "host certificate PEM")
	key := fs.String("key", "", "host key PEM")
	ca := fs.String("ca", "", "fleet CA PEM")
	vip := fs.String("vip", "", "VIP")
	replica := fs.String("replica", "", "replica id")
	vmIP := fs.String("vm-ip", "", "real server address")
	weight := fs.Int("weight", 0, "weight (PUT); 0 = drain")
	seq := fs.Int64("seq", time.Now().UnixNano(), "push sequence (latest wins)")
	del := fs.Bool("delete", false, "DELETE the member")
	reason := fs.String("reason", "", "reason")
	timeout := fs.Duration("timeout", 500*time.Millisecond, "per-director deadline")
	_ = fs.Parse(args)
	if *to == "" || *cert == "" || *key == "" || *ca == "" || *vip == "" || *replica == "" || *vmIP == "" {
		fmt.Fprintln(os.Stderr, "push: --to --cert --key --ca --vip --replica --vm-ip are required")
		return 2
	}
	creds, err := lbd.LoadFiles("", *key, *cert, *ca)
	if err != nil {
		fmt.Fprintln(os.Stderr, "push:", err)
		return 1
	}
	pool, err := pki.PoolFromPEM([]byte(creds.FleetCAPEM))
	if err != nil {
		fmt.Fprintln(os.Stderr, "push:", err)
		return 1
	}
	var eps []hostapi.DirectorEndpoint
	idList := strings.Split(*ids, ",")
	for i, addr := range strings.Split(*to, ",") {
		addr = strings.TrimSpace(addr)
		if addr == "" {
			continue
		}
		ep := hostapi.DirectorEndpoint{Address: addr, Port: lbd.DefaultPushPort}
		if h, p, err := splitHostPort(addr); err == nil {
			ep.Address, ep.Port = h, p
		}
		if i < len(idList) {
			ep.ID = strings.TrimSpace(idList[i])
		}
		eps = append(eps, ep)
	}
	tlsCert := creds.TLSCertificate()
	p := &lbd.Pusher{Cert: func() *tls.Certificate { return &tlsCert }, FleetCA: pool, Timeout: *timeout}
	method := http.MethodPut
	if *del {
		method = http.MethodDelete
	}
	res := p.Push(context.Background(), eps, method, *vip, *replica, hostapi.VIPMember{VMIP: *vmIP, Weight: *weight, Reason: *reason, Seq: *seq})
	rc := 0
	for _, r := range res {
		out := map[string]any{"director": r.Director, "latency_us": r.Latency.Microseconds()}
		if r.Err != nil {
			out["error"] = r.Err.Error()
			rc = 1
		}
		raw, _ := json.Marshal(out)
		fmt.Println(string(raw))
	}
	return rc
}

func splitHostPort(s string) (string, int, error) {
	i := strings.LastIndex(s, ":")
	if i < 0 {
		return s, lbd.DefaultPushPort, fmt.Errorf("no port")
	}
	p, err := strconv.Atoi(s[i+1:])
	if err != nil {
		return s, lbd.DefaultPushPort, err
	}
	return s[:i], p, nil
}

func renderKeepalived(args []string) int {
	fs := flag.NewFlagSet("render-keepalived", flag.ExitOnError)
	file := fs.String("desired-file", "", "desired state JSON")
	id := fs.String("id", "", "this director's id")
	addr := fs.String("address", "", "this director's address")
	iface := fs.String("iface", "eth0", "interface")
	bin := fs.String("bin", "/usr/local/bin/boss-lbd", "boss-lbd path for scripts")
	dir := fs.String("state-dir", "/run/boss-lbd", "run dir")
	_ = fs.Parse(args)
	raw, err := os.ReadFile(*file)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	var ds hostapi.DirectorDesired
	if err := json.Unmarshal(raw, &ds); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	if len(ds.Groups) == 0 {
		var vips, ids []string
		for _, v := range ds.VIPs {
			vips = append(vips, v.VIP)
		}
		for _, d := range ds.Directors {
			ids = append(ids, d.ID)
		}
		ds.Groups = lbd.Group(vips, ids)
	}
	var peer string
	if ds.Peer != nil {
		peer = ds.Peer.Address
	}
	fmt.Print(lbd.RenderKeepalived(lbd.KeepalivedConfig{SelfID: *id, SelfAddress: *addr, PeerAddress: peer, Iface: *iface,
		Groups: ds.Groups, TrackBin: *bin, StateDir: *dir}))
	return 0
}

var _ = x509.NewCertPool
