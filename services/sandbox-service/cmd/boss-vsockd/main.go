// boss-vsockd is the in-guest relay and control server (LLD §3.8). It holds
// no credential. In a Firecracker VM it runs as PID 1 (init=/sbin/boss-vsockd);
// for the process backend boss-hostd starts it as the stand-in VM process.
//
//	boss-vsockd                          VM mode: vsock transport, fixed 127.0.0.1:71xx
//	boss-vsockd --mode=process --dir=D   process backend: Unix sockets in D
//
// --boss-vm-id is a marker only: boss-hostd finds its VMs by it after a crash.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/seanheiney/boss/services/sandbox-service/internal/guest"
)

func main() {
	mode := flag.String("mode", "vm", "vm | process")
	dir := flag.String("dir", "", "process mode: the VM directory holding the sockets")
	noInbox := flag.Bool("no-inbox", false, "leave the work inbox (vsock 7105) to boss-runtime")
	_ = flag.String("boss-vm-id", "", "marker for boss-hostd's process scan")
	flag.Parse()
	if err := run(*mode, *dir, *noInbox); err != nil {
		fmt.Fprintln(os.Stderr, "boss-vsockd:", err)
		if os.Getpid() == 1 {
			select {} // PID 1 must not exit (kernel panic); the host watchdog sees no pings
		}
		os.Exit(1)
	}
}

func run(mode, dir string, noInbox bool) error {
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	d := &guest.Daemon{NoInbox: noInbox}
	switch mode {
	case "vm":
		if os.Getpid() == 1 {
			if err := guest.InitSystem(); err != nil {
				log.Printf("init: %v", err)
			}
		}
		d.T = guest.VsockTransport{}
		d.FixedLoopback = true
		d.NetSetup = guest.ReplicaNetSetup
	case "process":
		if dir == "" {
			return fmt.Errorf("--dir is required in process mode")
		}
		d.T = guest.UnixTransport{Dir: dir}
	default:
		return fmt.Errorf("unknown --mode %q", mode)
	}
	log.Printf("boss-vsockd up mode=%s", mode)
	return d.Run(ctx)
}
