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
// boss-hostd (cmd/boss-hostd) serves this same lane plus the P8 fleet agent.
package main

import (
	"log"
	"net/http"

	"github.com/seanheiney/boss/services/sandbox-service/internal/legacy"
)

func main() {
	srv, addr := legacy.Server("sandbox-service")
	if err := http.ListenAndServe(addr, srv.Handler()); err != nil {
		log.Fatal(err)
	}
}
