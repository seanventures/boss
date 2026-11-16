// boss-fakecp runs the Go fake control plane (internal/fakecp) as a process,
// for boss-1 runs of the P8a tests against a real boss-hostd. It is a test
// tool: it never runs in an install.
//
//	boss-fakecp --listen 127.0.0.1:9443 --dir D --join-token-file F
//
// It writes D/ca.pem (pin this in BOSS_HOSTD_CP_CA). Admin calls are loopback
// only: PUT /admin/desired/<host>, POST /admin/revoke/<host>, GET /admin/state.
package main

import (
	"flag"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"

	"github.com/seanventures/boss/services/sandbox-service/internal/fakecp"
)

func main() {
	listen := flag.String("listen", "127.0.0.1:9443", "listen address")
	dir := flag.String("dir", ".", "where to write ca.pem")
	tokFile := flag.String("join-token-file", "", "file holding a one-time join token to accept")
	flag.Parse()
	cp, err := fakecp.New()
	if err != nil {
		log.Fatal(err)
	}
	if *tokFile != "" {
		raw, err := os.ReadFile(*tokFile)
		if err != nil {
			log.Fatal(err)
		}
		cp.AddJoinToken(strings.TrimSpace(string(raw)))
	}
	if err := os.WriteFile(filepath.Join(*dir, "ca.pem"), []byte(cp.CA.PEM), 0o644); err != nil {
		log.Fatal(err)
	}
	if err := cp.Start(*listen); err != nil {
		log.Fatal(err)
	}
	log.Printf("boss-fakecp serving %s", cp.URL)
	ch := make(chan os.Signal, 1)
	signal.Notify(ch, syscall.SIGTERM, syscall.SIGINT)
	<-ch
	cp.Close()
}
