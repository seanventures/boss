// sandbox-service implements the Python ⇄ Go sandbox contract (v1.0) with two
// execution backends behind one interface:
//
//	BOSS_SANDBOX_BACKEND=""            fail-closed S1 scaffold (default): no
//	                                   execution; coding submits 422 with a
//	                                   named reason. Claims stay frozen unless
//	                                   BOSS_MICROVM_CLAIMS_FROZEN is explicitly
//	                                   false.
//	BOSS_SANDBOX_BACKEND="process"     runs each task's guest pipeline as a
//	                                   local subprocess — contract testing on
//	                                   any machine/CI; no isolation claim.
//	BOSS_SANDBOX_BACKEND="firecracker" one Firecracker microVM per task; needs
//	                                   /dev/kvm, the firecracker binary, and a
//	                                   kernel+rootfs baked by
//	                                   scripts/sandbox/microvm-setup.sh.
//
// See docs/infra/MICROVM_ENABLE.md for the operator enable/rollback runbook.
package main

import (
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/backend"
	"github.com/seanheiney/boss/services/sandbox-service/internal/httpapi"
	"github.com/seanheiney/boss/services/sandbox-service/internal/job"
	"github.com/seanheiney/boss/services/sandbox-service/internal/run"
)

func envBool(key string, def bool) bool {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return def
	}
	switch strings.ToLower(raw) {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return def
	}
}

func envInt(key string, def int) int {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return def
	}
	if n, err := strconv.Atoi(raw); err == nil && n > 0 {
		return n
	}
	return def
}

func buildBackend() backend.Backend {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("BOSS_SANDBOX_BACKEND"))) {
	case "", "none":
		return nil
	case "process":
		return &backend.Process{
			Python:    strings.TrimSpace(os.Getenv("BOSS_SANDBOX_PYTHON")),
			AgentPath: strings.TrimSpace(os.Getenv("BOSS_SANDBOX_GUEST_AGENT")),
		}
	case "firecracker":
		return &backend.Firecracker{
			Bin:       strings.TrimSpace(os.Getenv("BOSS_SANDBOX_FC_BIN")),
			Kernel:    strings.TrimSpace(os.Getenv("BOSS_SANDBOX_FC_KERNEL")),
			Rootfs:    strings.TrimSpace(os.Getenv("BOSS_SANDBOX_FC_ROOTFS")),
			Vcpus:     envInt("BOSS_SANDBOX_FC_VCPUS", 2),
			MemMB:     envInt("BOSS_SANDBOX_FC_MEM_MB", 2048),
			WorkMB:    envInt("BOSS_SANDBOX_FC_WORK_MB", 2048),
			TapDev:    strings.TrimSpace(os.Getenv("BOSS_SANDBOX_FC_TAP")),
			GuestIP:   strings.TrimSpace(os.Getenv("BOSS_SANDBOX_FC_GUEST_IP")),
			GatewayIP: strings.TrimSpace(os.Getenv("BOSS_SANDBOX_FC_GATEWAY_IP")),
		}
	default:
		log.Fatalf("unknown BOSS_SANDBOX_BACKEND=%q (want: process|firecracker|empty)",
			os.Getenv("BOSS_SANDBOX_BACKEND"))
		return nil
	}
}

func main() {
	// Default true: do not unfreeze Firecracker by accident.
	frozen := envBool("BOSS_MICROVM_CLAIMS_FROZEN", true)
	addr := strings.TrimSpace(os.Getenv("BOSS_SANDBOX_LISTEN"))
	if addr == "" {
		addr = "127.0.0.1:8078"
	}
	token := os.Getenv("BOSS_SANDBOX_TOKEN")
	store := job.NewStore(frozen)

	var srv *httpapi.Server
	b := buildBackend()
	if b == nil {
		srv = httpapi.New(store, token)
		log.Printf(
			"sandbox-service S1 scaffold listening on %s claims_frozen=%v backend=none firecracker_live=false (not GA)",
			addr, frozen,
		)
	} else {
		workRoot := strings.TrimSpace(os.Getenv("BOSS_SANDBOX_WORK_ROOT"))
		if workRoot == "" {
			workRoot = os.TempDir() + "/boss-sandbox"
		}
		if err := os.MkdirAll(workRoot, 0o755); err != nil {
			log.Fatalf("cannot create work root %s: %v", workRoot, err)
		}
		store.ConfigureBackend(b.Name(), b.Check)
		runner := run.New(store, b, workRoot)
		runner.MaxTimeout = time.Duration(envInt("BOSS_SANDBOX_MAX_TIMEOUT_SECONDS", 3600)) * time.Second
		srv = httpapi.NewWithRunner(store, token, runner)
		if err := b.Check(); err != nil {
			log.Printf("WARNING: backend %s is configured but NOT ready: %v", b.Name(), err)
		}
		log.Printf(
			"sandbox-service listening on %s claims_frozen=%v backend=%s ready=%v firecracker_live=%v work_root=%s",
			addr, frozen, b.Name(), b.Check() == nil, store.FirecrackerLive(), workRoot,
		)
	}
	if err := http.ListenAndServe(addr, srv.Handler()); err != nil {
		log.Fatal(err)
	}
}
