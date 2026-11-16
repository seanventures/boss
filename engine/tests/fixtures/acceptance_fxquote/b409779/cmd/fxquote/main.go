package main

import (
	"log"
	"net/http"
	"os"
	"time"

	"fxquote"
)

func main() {
	host := os.Getenv("HOST")
	if host == "" {
		host = "0.0.0.0"
	}
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	cfgPath := os.Getenv("FXQUOTE_CONFIG")
	if cfgPath == "" {
		cfgPath = "rates.yaml"
	}

	srv, err := fxquote.NewServer(cfgPath, 30*time.Second)
	if err != nil {
		log.Fatalf("fxquote: failed to start: %v", err)
	}

	addr := host + ":" + port
	log.Printf("fxquote listening on %s", addr)
	if err := http.ListenAndServe(addr, srv.Handler()); err != nil {
		log.Fatalf("fxquote: server error: %v", err)
	}
}
