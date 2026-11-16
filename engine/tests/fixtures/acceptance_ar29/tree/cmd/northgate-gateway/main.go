package main

import (
	"crypto/tls"
	"crypto/x509"
	"log"
	"net/http"
	"os"
	"time"

	"northgate/internal/auth"
	"northgate/internal/config"
	"northgate/internal/metrics"
	"northgate/internal/proxy"
	"northgate/internal/ratelimit"
)

const version = "0.1.0"

// readyzHandler reports the tri-state readiness the PRD requires: 503
// "config not loaded" before any config has ever loaded, 200 "ready" once
// one has (whether or not a later reload was rejected -- state.Ready()
// never un-sets once true, and the rejection itself is logged separately
// by config.ReloadOnce/Watch).
func readyzHandler(state *config.ReloadState) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if state.Ready() {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("ready"))
			return
		}
		w.WriteHeader(http.StatusServiceUnavailable)
		w.Write([]byte("config not loaded"))
	}
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func main() {
	host := getenv("HOST", "0.0.0.0")
	port := getenv("PORT", "8080")
	configDir := getenv("CONFIG_DIR", "config")

	store := &config.Store{}
	state := &config.ReloadState{}
	limiter := ratelimit.NewLimiter(1000, 1000)

	reg := metrics.NewRegistry()
	if err := config.ReloadOnce(configDir, store, state, reg); err != nil {
		// A broken initial config must not crash the process: start
		// unready (/readyz reports 503 "config not loaded") rather than
		// aborting, so the gateway can still be fixed by a later reload
		// instead of needing a restart.
		log.Printf("initial config load failed, starting unready: %v", err)
	}

	go config.Watch(configDir, 300*time.Millisecond, store, state, reg, func(snap *config.Snapshot, err error) {
		if err == nil {
			limiter.Reset()
		}
	})

	jwks := auth.NewJWKSCache()
	handler := proxy.NewHandler(store, jwks, limiter)
	proxy.SetPIIFieldsLookup(func(tenant string) []string {
		snap := store.Current()
		if snap == nil {
			return nil
		}
		if t, ok := snap.Tenants[tenant]; ok {
			return t.PIIFields
		}
		return nil
	})

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("ok"))
	})
	mux.HandleFunc("/readyz", readyzHandler(state))
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		metrics.RenderMetricsHandler(reg, version)(w, r)
	})
	mux.Handle("/", handler)

	instrumented := metrics.Middleware(reg, "/metrics", handler.RouteTemplate, mux)

	addr := host + ":" + port

	// mTLS is opt-in per deployment: setting MTLS_CERT_FILE/MTLS_KEY_FILE
	// (and, for requiring client certs, MTLS_CLIENT_CA_FILE) switches this
	// instance's listener to TLS with client-certificate support, which
	// routes with auth.type: mtls (and partner-sensitive tenants) need --
	// without it, r.TLS is always nil and such routes could never pass.
	certFile, keyFile := getenv("MTLS_CERT_FILE", ""), getenv("MTLS_KEY_FILE", "")
	if certFile != "" && keyFile != "" {
		tlsConfig := &tls.Config{ClientAuth: tls.RequestClientCert}
		if clientCAFile := getenv("MTLS_CLIENT_CA_FILE", ""); clientCAFile != "" {
			caPEM, err := os.ReadFile(clientCAFile)
			if err != nil {
				log.Fatalf("MTLS_CLIENT_CA_FILE %q unreadable: %v", clientCAFile, err)
			}
			pool := x509.NewCertPool()
			if !pool.AppendCertsFromPEM(caPEM) {
				log.Fatalf("MTLS_CLIENT_CA_FILE %q contains no usable certificate", clientCAFile)
			}
			tlsConfig.ClientCAs = pool
			tlsConfig.ClientAuth = tls.VerifyClientCertIfGiven
		}
		server := &http.Server{Addr: addr, Handler: instrumented, TLSConfig: tlsConfig}
		log.Printf("northgate-gateway listening on %s with mTLS support (config dir %s)", addr, configDir)
		if err := server.ListenAndServeTLS(certFile, keyFile); err != nil {
			log.Fatalf("server error: %v", err)
		}
		return
	}

	log.Printf("northgate-gateway listening on %s (config dir %s)", addr, configDir)
	if err := http.ListenAndServe(addr, instrumented); err != nil {
		log.Fatalf("server error: %v", err)
	}
}
