// sandbox-service is the in-repo S1 scaffold.
//
// It implements the MicroVMProvider HTTP contract with typed jobs, leases,
// and fail-closed errors. It does NOT boot Firecracker. Claims stay frozen
// unless BOSS_MICROVM_CLAIMS_FROZEN is explicitly false — and even then
// submit still fails closed with firecracker_not_enabled.
package main

import (
	"log"
	"net/http"
	"os"
	"strings"

	"github.com/seanheiney/boss/services/sandbox-service/internal/httpapi"
	"github.com/seanheiney/boss/services/sandbox-service/internal/job"
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

func main() {
	// Default true: do not unfreeze Firecracker by accident.
	frozen := envBool("BOSS_MICROVM_CLAIMS_FROZEN", true)
	addr := strings.TrimSpace(os.Getenv("BOSS_SANDBOX_LISTEN"))
	if addr == "" {
		addr = "127.0.0.1:8078"
	}
	token := os.Getenv("BOSS_SANDBOX_TOKEN")
	store := job.NewStore(frozen)
	srv := httpapi.New(store, token)
	log.Printf(
		"sandbox-service S1 scaffold listening on %s claims_frozen=%v firecracker_live=false (not GA)",
		addr, frozen,
	)
	if err := http.ListenAndServe(addr, srv.Handler()); err != nil {
		log.Fatal(err)
	}
}
