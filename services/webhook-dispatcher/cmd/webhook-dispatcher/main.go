// webhook-dispatcher is the optional BOSS-102 S3 fan-out sidecar.
//
// Default is dry-run: no outbound network. The Python engine remains the
// event source of truth and works when this process is not running.
package main

import (
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/seanheiney/boss/services/webhook-dispatcher/internal/dispatch"
)

func envBool(name string, fallback bool) bool {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	switch strings.ToLower(raw) {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return fallback
	}
}

func main() {
	listen := strings.TrimSpace(os.Getenv("BOSS_GO_WEBHOOK_DISPATCHER_LISTEN"))
	if listen == "" {
		listen = ":8091"
	}
	dryRun := envBool("BOSS_GO_WEBHOOK_DISPATCHER_DRY_RUN", true)
	token := strings.TrimSpace(os.Getenv("BOSS_GO_WEBHOOK_DISPATCHER_TOKEN"))
	cfg := dispatch.Config{
		DryRun:      dryRun,
		MaxAttempts: 3,
		Backoff:     200 * time.Millisecond,
		Timeout:     10 * time.Second,
		LookupEnv:   os.LookupEnv,
		Log:         log.Writer(),
	}
	d := dispatch.New(cfg, dispatch.NewMemoryStore(), dispatch.HTTPPoster{Timeout: cfg.Timeout})
	log.Printf("webhook-dispatcher listen=%s dry_run=%t ga=false s3_live=false", listen, dryRun)
	if err := http.ListenAndServe(listen, dispatch.NewHandler(d, token)); err != nil {
		log.Fatal(err)
	}
}
