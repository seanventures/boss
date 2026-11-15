// boss-hostd is the P8 fleet host service: today's sandbox-service plus the
// fleet agent (LLD §3.24). It always serves the legacy coding lane
// (/v1/sandbox/*, configured by BOSS_SANDBOX_*), and when BOSS_HOSTD_CP_URL is
// set it also enrols with the control plane and runs VMs from desired state.
//
//	boss-hostd                         legacy lane (+ fleet agent if configured)
//	boss-hostd netadm --socket S --allow-uid U
//	                                   the CAP_NET_ADMIN helper (Linux): taps,
//	                                   routes and nftables for replica VMs
//
// Fleet environment (see docs/infra/HOST_API.md):
//
//	BOSS_HOSTD_CP_URL            control-plane base URL (https)
//	BOSS_HOSTD_CP_CA             PEM file: the pinned BOSS CA
//	BOSS_HOSTD_JOIN_TOKEN_FILE   one-time join token (first start only)
//	BOSS_HOSTD_STATE_DIR         pki/ + cache.db   (default /var/lib/boss-hostd)
//	BOSS_HOSTD_RUN_DIR           per-VM dirs       (default /run/boss-hostd/vms)
//	BOSS_HOSTD_VM_BACKEND        process | firecracker
//	BOSS_HOSTD_GUEST_BIN         boss-vsockd (process backend)
//	BOSS_HOSTD_FC_ROOTFS         base rootfs with /sbin/boss-vsockd (firecracker)
//	BOSS_SANDBOX_FC_BIN, BOSS_SANDBOX_FC_KERNEL   shared with the legacy lane
//	BOSS_HOSTD_NETADM_SOCKET     helper socket; "inprocess" if hostd has CAP_NET_ADMIN
//	BOSS_HOSTD_MAX_VMS           default 20
//	BOSS_HOSTD_POOL              host pool label
//	BOSS_HOSTD_STRICT_SECRETS    default true
//	BOSS_HOSTD_HEADLESS_MAX_SECONDS  default 86400: control plane unreachable this long → stop serving
//	BOSS_MICROVM_CLAIMS_FROZEN   default true: gates per_run VMs (replicas are on)
//
// P8b (snapshots, warm pools, jailer; docs/infra/HOST_API.md §3.4, §8):
//
//	BOSS_HOSTD_JAILER_BIN        Firecracker's jailer; set → every VM runs jailed (needs root)
//	BOSS_HOSTD_JAILER_CHROOT     jail base (default /srv/boss-jail; keep it short)
//	BOSS_HOSTD_JAILER_UID_BASE   first uid of the per-VM uid/gid pool (default 100000, range 10000)
//	BOSS_HOSTD_SNAP_DIR          snapshot store (default <state dir>/snapshots)
//	BOSS_HOSTD_OVERLAY_MIB       per-VM writable overlay drive (default 1024; 0 = none)
//	BOSS_HOSTD_RUNTIME_VERSION   baked boss-runtime version (part of the snapshot key)
//	BOSS_HOSTD_REPLICA_SURGE     warm spares per replica shape (default 1)
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/hostd"
	"github.com/seanheiney/boss/services/sandbox-service/internal/legacy"
	"github.com/seanheiney/boss/services/sandbox-service/internal/netpol"
	"github.com/seanheiney/boss/services/sandbox-service/internal/vmm"
)

func env(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "netadm" {
		os.Exit(netadm(os.Args[2:]))
	}
	if len(os.Args) > 1 && os.Args[1] == "netpol-render" {
		os.Exit(netpolRender(os.Args[2:]))
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()

	srv, addr := legacy.Server("boss-hostd")
	if os.Getenv("BOSS_HOSTD_CP_URL") != "" {
		agent, err := buildAgent()
		if err != nil {
			log.Fatalf("boss-hostd: %v", err)
		}
		srv.Fleet = agent
		go func() {
			if err := agent.Run(ctx); err != nil {
				log.Fatalf("boss-hostd fleet agent: %v", err)
			}
		}()
	}
	hs := &http.Server{Addr: addr, Handler: srv.Handler()}
	go func() { <-ctx.Done(); _ = hs.Close() }()
	if err := hs.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}

func buildAgent() (*hostd.Agent, error) {
	caPath := env("BOSS_HOSTD_CP_CA", "")
	if caPath == "" {
		return nil, fmt.Errorf("BOSS_HOSTD_CP_CA (the pinned control-plane CA) is required")
	}
	ca, err := os.ReadFile(caPath)
	if err != nil {
		return nil, fmt.Errorf("read pinned CA: %w", err)
	}
	var join string
	joinFile := env("BOSS_HOSTD_JOIN_TOKEN_FILE", "")
	if p := joinFile; p != "" {
		raw, err := os.ReadFile(p)
		if err != nil && !os.IsNotExist(err) {
			return nil, fmt.Errorf("read join token file: %w", err)
		}
		join = strings.TrimSpace(string(raw))
	}
	runDir := env("BOSS_HOSTD_RUN_DIR", "/run/boss-hostd/vms")
	if err := os.MkdirAll(runDir, 0o700); err != nil {
		return nil, err
	}
	var mgr vmm.Manager
	switch env("BOSS_HOSTD_VM_BACKEND", "process") {
	case "process":
		mgr = &vmm.Process{Root: runDir, GuestBin: env("BOSS_HOSTD_GUEST_BIN", ""),
			SnapDir:        env("BOSS_HOSTD_SNAP_DIR", filepath.Join(env("BOSS_HOSTD_STATE_DIR", "/var/lib/boss-hostd"), "snapshots")),
			RuntimeVersion: env("BOSS_HOSTD_RUNTIME_VERSION", "")}
	case "firecracker":
		fc := &vmm.Firecracker{Root: runDir, Bin: env("BOSS_SANDBOX_FC_BIN", ""),
			Kernel: env("BOSS_SANDBOX_FC_KERNEL", ""), Rootfs: env("BOSS_HOSTD_FC_ROOTFS", ""),
			SnapDir:    env("BOSS_HOSTD_SNAP_DIR", filepath.Join(env("BOSS_HOSTD_STATE_DIR", "/var/lib/boss-hostd"), "snapshots")),
			OverlayMiB: legacy.EnvInt("BOSS_HOSTD_OVERLAY_MIB", 1024), RuntimeVersion: env("BOSS_HOSTD_RUNTIME_VERSION", "")}
		if jb := env("BOSS_HOSTD_JAILER_BIN", ""); jb != "" {
			fc.Jailer = &vmm.Jailer{Bin: jb, ChrootBase: env("BOSS_HOSTD_JAILER_CHROOT", "/srv/boss-jail"),
				UIDBase: legacy.EnvInt("BOSS_HOSTD_JAILER_UID_BASE", 100000)}
		}
		mgr = fc
	default:
		return nil, fmt.Errorf("unknown BOSS_HOSTD_VM_BACKEND (want process|firecracker)")
	}
	var nm netpol.Manager = netpol.Noop{}
	if mgr.Name() == "firecracker" {
		sock := env("BOSS_HOSTD_NETADM_SOCKET", "/run/boss-hostd/netadm.sock")
		if sock == "inprocess" {
			// Only when hostd itself holds CAP_NET_ADMIN; the policy then
			// comes from hostd's own environment.
			nm = inProcessNet(netpol.Policy{VMPrefix: env("BOSS_HOSTD_VM_PREFIX", ""), TapOwnerUID: os.Getuid()})
		} else {
			nm = netpol.HelperClient{Socket: sock}
		}
	}
	host, _ := os.Hostname()
	return hostd.New(hostd.Config{
		StateDir:       env("BOSS_HOSTD_STATE_DIR", "/var/lib/boss-hostd"),
		CPURL:          env("BOSS_HOSTD_CP_URL", ""),
		PinnedCA:       ca,
		JoinToken:      join,
		JoinTokenFile:  joinFile,
		HeadlessMax:    time.Duration(legacy.EnvInt("BOSS_HOSTD_HEADLESS_MAX_SECONDS", 24*3600)) * time.Second,
		Hostname:       host,
		Pool:           env("BOSS_HOSTD_POOL", "default"),
		Manager:        mgr,
		Net:            nm,
		ClaimsFrozen:   legacy.EnvBool("BOSS_MICROVM_CLAIMS_FROZEN", true),
		StrictSecrets:  legacy.EnvBool("BOSS_HOSTD_STRICT_SECRETS", true),
		MaxVMs:         legacy.EnvInt("BOSS_HOSTD_MAX_VMS", 20),
		TapOwnerUID:    os.Getuid(),
		RuntimeVersion: env("BOSS_HOSTD_RUNTIME_VERSION", ""),
		ReplicaSurge:   replicaSurge(),
	})
}

// replicaSurge reads BOSS_HOSTD_REPLICA_SURGE ("0" disables the spare).
func replicaSurge() int {
	raw := env("BOSS_HOSTD_REPLICA_SURGE", "1")
	n, err := strconv.Atoi(raw)
	if err != nil {
		return 1
	}
	if n <= 0 {
		return -1 // explicit 0: no spare
	}
	return n
}

func netadm(args []string) int {
	fs := flag.NewFlagSet("netadm", flag.ExitOnError)
	sock := fs.String("socket", "/run/boss-hostd/netadm.sock", "Unix socket to serve")
	uid := fs.Int("allow-uid", -1, "the only uid allowed to connect (boss-hostd's)")
	prefix := fs.String("vm-prefix", "", "this host's VM block (CIDR); every vm_ip must be inside it")
	owner := fs.Int("tap-owner-uid", -1, "the only owner a tap may get (default: --allow-uid)")
	internal := fs.String("internal-nets", "", "extra comma-separated CIDRs no VM may reach (infra ranges)")
	_ = fs.Parse(args)
	if *uid < 0 || *prefix == "" {
		fmt.Fprintln(os.Stderr, "netadm: --allow-uid and --vm-prefix are required")
		return 2
	}
	if *owner < 0 {
		*owner = *uid
	}
	pol := netpol.Policy{VMPrefix: *prefix, TapOwnerUID: *owner}
	for _, n := range strings.Split(*internal, ",") {
		if n = strings.TrimSpace(n); n != "" {
			pol.InternalNets = append(pol.InternalNets, n)
		}
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	if err := serveNetadm(ctx, *sock, *uid, pol); err != nil {
		fmt.Fprintln(os.Stderr, "netadm:", err)
		return 1
	}
	return 0
}

// netpolRender prints the exact nftables script the helper applies for one
// replica (base table, director set, the VM's chains). Diagnostics and the
// netns lab (scripts/sandbox/hostd-netlab.sh); it changes nothing.
func netpolRender(args []string) int {
	fs := flag.NewFlagSet("netpol-render", flag.ExitOnError)
	tap := fs.String("tap", "", "tap name (bt + 12 hex)")
	vmIP := fs.String("vm-ip", "", "the replica's /32")
	vip := fs.String("vip", "", "the agent VIP")
	ports := fs.String("vip-ports", "443", "comma-separated VIP ports")
	prefix := fs.String("vm-prefix", "", "the host's VM block")
	directors := fs.String("directors", "", "comma-separated director IPs")
	internal := fs.String("internal-nets", "", "extra internal CIDRs")
	_ = fs.Parse(args)
	v := netpol.VMNet{Tap: *tap, VMIP: *vmIP, VIP: *vip, VMPrefix: *prefix}
	for _, p := range strings.Split(*ports, ",") {
		if n, err := strconv.Atoi(strings.TrimSpace(p)); err == nil {
			v.VIPPorts = append(v.VIPPorts, n)
		}
	}
	if err := v.Validate(); err != nil {
		fmt.Fprintln(os.Stderr, "netpol-render:", err)
		return 2
	}
	pol := netpol.Policy{VMPrefix: *prefix}
	for _, n := range strings.Split(*internal, ",") {
		if n = strings.TrimSpace(n); n != "" {
			pol.InternalNets = append(pol.InternalNets, n)
		}
	}
	var ds []string
	for _, d := range strings.Split(*directors, ",") {
		if d = strings.TrimSpace(d); d != "" {
			ds = append(ds, d)
		}
	}
	dir, err := netpol.RenderDirectors(ds)
	if err != nil {
		fmt.Fprintln(os.Stderr, "netpol-render:", err)
		return 2
	}
	fmt.Print(netpol.RenderBase(pol.Internal()), dir, netpol.RenderVM(v))
	return 0
}
