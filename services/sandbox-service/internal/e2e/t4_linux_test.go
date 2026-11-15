//go:build linux

package e2e

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/guest"
	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanheiney/boss/services/sandbox-service/internal/netpol"
	"github.com/seanheiney/boss/services/sandbox-service/internal/vmm"
)

func control(t *testing.T, m vmm.Manager, id, op string) *guest.Response {
	t.Helper()
	vms, _ := m.Scan()
	for _, vm := range vms {
		if vm.ID != id {
			continue
		}
		c, err := m.DialControl(vm, time.Now().Add(10*time.Second))
		if err != nil {
			t.Fatalf("%s control: %v", id, err)
		}
		defer c.Close()
		r, err := guest.Exchange(c, guest.Request{Op: op}, time.Now().Add(10*time.Second))
		if err != nil || !r.OK {
			t.Fatalf("%s %s: %v %+v", id, op, err, r)
		}
		return r
	}
	t.Fatalf("VM %s not running", id)
	return nil
}

// P8a-T4 on real KVM: with N replica VMs (default 20) and one per_run VM, no
// replica originates a single packet on its tap (the nftables drop counters
// stay at zero) while models/tools/run-plane calls work over vsock; a per_run
// VM has only lo. A deliberate egress attempt from one replica proves the
// counters count and the policy drops.
func TestP8aT4NoVMOriginatesAPacket(t *testing.T) {
	if os.Getenv("BOSS_E2E_BACKEND") != "firecracker" {
		t.Skip("T4 needs BOSS_E2E_BACKEND=firecracker on a KVM host")
	}
	t.Setenv("BOSS_E2E_FROZEN", "false") // this test exercises per_run explicitly
	n, _ := strconv.Atoi(envOr("BOSS_E2E_REPLICAS", "20"))
	e := newEnv(t)
	seen := runPlane(e)
	e.startHostd()
	e.waitFor(10*time.Second, "enrolment", func() bool { return e.hostID() != "" })
	ds := desired(n, "10.200.4.0/23", false, nil) // run_plane = "/v1/run-plane" on the CP
	ds.VMs = append(ds.VMs, hostapi.DesiredVM{ReplicaID: "rep_run01", VMID: "vm_run01", DeploymentID: "coder",
		Mode: hostapi.ModePerRun, State: hostapi.DesiredRunning, ImageDigest: "sha256:p8a-agent",
		Resources: hostapi.Resources{"vcpu": 1.0, "mem_mib": 128.0},
		Runs:      []hostapi.RunBind{{RunID: "run_e2e", Epoch: 1, Handles: hostapi.Handles{Run: "boss_run_e2e", Model: "boss_mdl_e2e"}}}})
	e.cp.SetDesired(e.hostID(), ds)
	took := e.waitFor(180*time.Second, fmt.Sprintf("%d replicas + 1 per_run serving", n), func() bool {
		return len(e.servingSince(time.Time{})) == n+1
	})
	t.Logf("%d replica VMs + 1 per_run VM serving on real KVM in %s", n, took)
	e.waitFor(15*time.Second, "attested heartbeat", func() bool {
		hb, _ := e.cp.LastHeartbeat(e.hostID())
		return hb != nil && hb.Facts.NetPolicyOK
	})
	hb, _ := e.cp.LastHeartbeat(e.hostID())
	f := hb.Facts
	if !f.KVM || !f.FirecrackerLive || !f.Seccomp || !f.StrictSecrets || !f.NetPolicyOK {
		t.Fatalf("heartbeat facts on KVM: %+v", f)
	}
	if f.Jailer != (os.Getenv("BOSS_E2E_JAILER_BIN") != "") {
		t.Fatalf("jailer_on must be measured, not configured: %+v", f)
	}
	t.Logf("heartbeat facts: kvm=%v firecracker_live=%v seccomp=%v jailer=%v smt_off=%v ksm_off=%v swap_off=%v strict=%v net_policy_ok=%v reasons=%v",
		f.KVM, f.FirecrackerLive, f.Seccomp, f.Jailer, f.SMTOff, f.KSMOff, f.SwapOff, f.StrictSecrets, f.NetPolicyOK, f.Reasons)

	// Guest-originated work over vsock: every VM calls the run plane.
	for i := 1; i <= n; i++ {
		id := fmt.Sprintf("vm_%02d", i)
		r := control(t, e.mgr, id, "selfcheck_gateway")
		if r.Selfcheck["status"] != "200" {
			t.Fatalf("%s gateway over vsock: %+v\n%s", id, r.Selfcheck, e.log())
		}
	}
	if r := control(t, e.mgr, "vm_run01", "selfcheck_gateway"); r.Selfcheck["status"] != "200" {
		t.Fatalf("per_run gateway over vsock: %+v", r.Selfcheck)
	}
	if bad := seen.withAuth(); bad > 0 {
		t.Fatalf("%d upstream requests carried a guest Authorization header", bad)
	}
	if bad := seen.wrongReplica(); bad > 0 {
		t.Fatalf("%d run-plane calls carried a replica id other than the socket's", bad)
	}
	t.Logf("%d run-plane calls over vsock reached the control plane under the host proof; 0 carried guest auth, 0 a forged replica id", seen.count())

	// The counters after steady state plus that traffic: all zero.
	nc := netClient()
	time.Sleep(3 * time.Second)
	for i := 1; i <= n; i++ {
		tap := netpol.TapName(fmt.Sprintf("vm_%02d", i))
		c, err := nc.Counters(context.Background(), tap)
		if err != nil {
			t.Fatal(err)
		}
		if c.Originated != 0 || c.Metadata != 0 {
			t.Fatalf("vm_%02d originated packets on %s: %+v", i, tap, c)
		}
	}
	t.Logf("all %d taps: orig=0 meta=0 after steady state and %d vsock calls", n, n+1)

	// per_run: only lo; nothing reachable.
	ifs := control(t, e.mgr, "vm_run01", "ifaces").Ifaces
	if strings.Join(ifs, ",") != "lo" {
		t.Fatalf("per_run VM interfaces: %v (want only lo)", ifs)
	}
	sc := control(t, e.mgr, "vm_run01", "selfcheck_egress").Selfcheck
	for k, v := range sc {
		if !strings.HasPrefix(v, "blocked") {
			t.Fatalf("per_run egress %s: %s", k, v)
		}
	}
	t.Logf("per_run VM: interfaces=%v, egress=%v", ifs, sc)

	// Positive control: rep-01 deliberately tries to get out. Under the
	// jailer (P8b) Firecracker's MMDS answers 169.254.169.254 inside the VM
	// with non-secret data, so that probe connects without a frame ever
	// reaching the tap; every other probe must die at the host end.
	mmds := os.Getenv("BOSS_E2E_JAILER_BIN") != ""
	sc = control(t, e.mgr, "vm_01", "selfcheck_egress").Selfcheck
	for k, v := range sc {
		if mmds && strings.Contains(k, "169.254.169.254") && v == "CONNECTED" {
			continue
		}
		if v == "CONNECTED" || v == "REPLY" || v == "RESOLVED" {
			t.Fatalf("replica egress escaped: %s=%s", k, v)
		}
	}
	time.Sleep(500 * time.Millisecond)
	c1, _ := nc.Counters(context.Background(), netpol.TapName("vm_01"))
	if c1.Originated == 0 || (c1.Metadata == 0) != mmds {
		t.Fatalf("deliberate egress was not counted as expected (mmds=%v): %+v (guest saw %v)", mmds, c1, sc)
	}
	for i := 2; i <= n; i++ {
		c, _ := nc.Counters(context.Background(), netpol.TapName(fmt.Sprintf("vm_%02d", i)))
		if c.Originated != 0 || c.Metadata != 0 {
			t.Fatalf("vm_%02d counters moved: %+v", i, c)
		}
	}
	t.Logf("positive control vm_01: guest=%v counters=%+v; other %d taps still 0", sc, c1, n-1)
}

type upLog struct {
	mu      sync.Mutex
	total   int
	auth    int
	replica int
}

func (u *upLog) count() int        { u.mu.Lock(); defer u.mu.Unlock(); return u.total }
func (u *upLog) withAuth() int     { u.mu.Lock(); defer u.mu.Unlock(); return u.auth }
func (u *upLog) wrongReplica() int { u.mu.Lock(); defer u.mu.Unlock(); return u.replica }

// runPlane serves /v1/run-plane/* on the fake control plane (host-proof
// authenticated, as the Python run plane is) and records what arrived.
func runPlane(e *env) *upLog {
	log := &upLog{}
	e.cp.RunPlane = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		log.mu.Lock()
		log.total++
		if r.Header.Get("Cookie") != "" || r.Header.Get("X-Boss-Run-Id") != "" ||
			!strings.HasPrefix(r.Header.Get("Authorization"), "BossHost ") {
			log.auth++
		}
		if rep := r.Header.Get("X-Boss-Replica"); rep == "" || rep == "forged-replica" {
			log.replica++
		}
		log.mu.Unlock()
		fmt.Fprintf(w, `{"ok":true}`)
	})
	return log
}
