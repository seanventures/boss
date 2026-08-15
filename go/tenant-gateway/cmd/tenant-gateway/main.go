// Command tenant-gateway is the optional S2 edge binary.
//
// It is not mounted on goldeneye. Production routing stays Traefik.
// The Python engine does not import or require this process.
package main

import (
	"flag"
	"log"
	"net/http"
	"os"

	gateway "github.com/seanheiney/boss/go/tenant-gateway"
)

func main() {
	listen := flag.String("listen", envOr("BOSS_GATEWAY_LISTEN", "127.0.0.1:8787"), "listen address")
	configPath := flag.String("config", envOr("BOSS_GATEWAY_CONFIG", ""), "path to tenants JSON")
	flag.Parse()

	var cfg gateway.Config
	if *configPath != "" {
		file, err := gateway.LoadConfigFile(*configPath)
		if err != nil {
			log.Fatalf("tenant-gateway: load config: %v", err)
		}
		cfg = file.Config()
		if file.Listen != "" && os.Getenv("BOSS_GATEWAY_LISTEN") == "" && !flagPassed("listen") {
			*listen = file.Listen
		}
	}

	gw, err := gateway.New(cfg)
	if err != nil {
		log.Fatalf("tenant-gateway: %v", err)
	}

	log.Printf("boss-tenant-gateway listen=%s tenants=%d (not a live goldeneye cutover)", *listen, len(cfg.Tenants))
	if err := http.ListenAndServe(*listen, gw); err != nil {
		log.Fatalf("tenant-gateway: %v", err)
	}
}

func envOr(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func flagPassed(name string) bool {
	found := false
	flag.Visit(func(f *flag.Flag) {
		if f.Name == name {
			found = true
		}
	})
	return found
}
