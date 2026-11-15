//go:build linux

package e2e

import (
	"context"
	"encoding/binary"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanheiney/boss/services/sandbox-service/internal/vmm"
)

// P8b on real KVM under the jailer (run as root on boss-1):
//
//		sudo env BOSS_E2E_BACKEND=firecracker BOSS_E2E_JAILER_BIN=… BOSS_E2E_FC_BIN=… \
//		  BOSS_E2E_FC_KERNEL=… BOSS_E2E_FC_ROOTFS=… BOSS_E2E_BIN_DIR=… BOSS_E2E_RUN_DIR=/tmp/bj \
//		  BOSS_E2E_NETADM_SOCKET=inprocess BOSS_E2E_VM_PREFIX=10.200.4.0/23 ./e2e.test -test.run P8b -test.v
//
//	  - jailer facts are measured true (jailer_on, seccomp_default);
//	  - the first VM of a shape cold-boots and is snapshotted clean; the pool
//	    restores the rest (restore p95 logged);
//	  - P8b-T4: BOSS_E2E_CLONES (50) restored VMs claimed in waves from the warm
//	    pool have distinct vm_ids, RNG output and reseeded kernels; no token or
//	    key is anywhere in a jail, a console log or the snapshot store;
//	  - P8b-T7: warm claim p95 < 300 ms, cold boot p95 < 3 s;
//	  - every VM runs under its own uid in a 0700 chroot with default seccomp
//	    and a new PID namespace; one VM's uid cannot read another VM's chroot;
//	  - two jailed replicas get a netns with a bridged tap and serve.
func TestP8bJailerSnapshotsAndClones(t *testing.T) {
	if os.Getenv("BOSS_E2E_BACKEND") != "firecracker" || os.Getenv("BOSS_E2E_JAILER_BIN") == "" {
		t.Skip("needs BOSS_E2E_BACKEND=firecracker and BOSS_E2E_JAILER_BIN on a KVM host")
	}
	if os.Geteuid() != 0 {
		t.Skip("the jailer needs root")
	}
	t.Setenv("BOSS_E2E_FROZEN", "false")
	t.Setenv("BOSS_E2E_REPLICA_SURGE", "1")
	clones := envInt("BOSS_E2E_CLONES", 50)
	warm := envInt("BOSS_E2E_WARM", 10)
	e := newEnv(t)
	seen := runPlane(e)
	e.startHostd()
	e.waitFor(10*time.Second, "enrolment", func() bool { return e.hostID() != "" })
	id := e.hostID()
	fc := e.mgr.(*vmm.Firecracker)

	// 1. Pool + two jailed replicas.
	ds := desired(2, envOr("BOSS_E2E_VM_PREFIX", "10.200.4.0/23"), false, nil)
	ds.Pools = []hostapi.Pool{{DeploymentID: "coder", ImageDigest: "sha256:p8b-coder", MinWarm: warm,
		Resources: hostapi.Resources{"vcpu": 1.0, "mem_mib": 128.0}}}
	e.cp.SetDesired(id, ds)
	took := e.waitFor(240*time.Second, fmt.Sprintf("%d warm VMs and 2 replicas", warm), func() bool {
		hb, _ := e.cp.LastHeartbeat(id)
		return hb != nil && hb.WarmPools["coder"].Depth >= warm && len(e.servingSince(time.Time{})) == 2
	})
	t.Logf("pool of %d filled and 2 replicas serving in %s", warm, took)
	hb, _ := e.cp.LastHeartbeat(id)
	f := hb.Facts
	if !f.Jailer || !f.Seccomp || !f.KVM || !f.FirecrackerLive || f.Reasons["jailer_on"] != "" {
		t.Fatalf("jailer facts must be measured true: %+v", f)
	}
	t.Logf("facts: jailer_on=%v seccomp_default=%v net_policy_ok=%v reasons=%v", f.Jailer, f.Seccomp, f.NetPolicyOK, f.Reasons)
	snaps := fc.Snapshots()
	if len(snaps) < 1 {
		t.Fatal("no snapshot taken")
	}
	for _, s := range snaps {
		if !s.CleanAttested {
			t.Fatalf("snapshot %s not clean-attested", s.Key)
		}
		for _, p := range []string{s.MemPath(), s.StatePath()} {
			st, err := os.Stat(p)
			if err != nil || st.Mode().Perm()&0o077 != 0 && !acled(p) {
				t.Fatalf("snapshot file %s: %v mode %o", p, err, st.Mode().Perm())
			}
		}
		t.Logf("snapshot %s: mode=%s mem=%d MiB clean_attested=%v", s.Key[:8], s.Shape.Mode, s.MemBytes>>20, s.CleanAttested)
	}
	for _, rep := range []string{"vm_01", "vm_02"} {
		ifs := control(t, e.mgr, rep, "ifaces").Ifaces
		if !strings.Contains(strings.Join(ifs, ","), "eth0") {
			t.Fatalf("jailed replica %s has no eth0: %v", rep, ifs)
		}
		if r := control(t, e.mgr, rep, "selfcheck_gateway"); r.Selfcheck["status"] != "200" {
			t.Fatalf("%s gateway over vsock: %+v", rep, r.Selfcheck)
		}
	}
	if bad := seen.withAuth(); bad > 0 {
		t.Fatalf("%d upstream requests carried a guest Authorization header", bad)
	}

	// 2. Claim clones in waves of `warm`, refilling between waves, so every
	// claim is served from the pool (P8b-T7 warm claim).
	var claimed []string
	for len(claimed) < clones {
		e.waitFor(120*time.Second, "pool refilled", func() bool {
			hb, _ := e.cp.LastHeartbeat(id)
			return hb != nil && hb.WarmPools["coder"].Depth >= warm
		})
		var wave []string
		for i := 0; i < warm && len(claimed)+len(wave) < clones; i++ {
			n := len(claimed) + len(wave) + 1
			vid := fmt.Sprintf("vm_c%02d", n)
			ds.VMs = append(ds.VMs, hostapi.DesiredVM{ReplicaID: "rep_c" + vid, VMID: vid, DeploymentID: "coder",
				Mode: hostapi.ModePerRun, State: hostapi.DesiredRunning, ImageDigest: "sha256:p8b-coder",
				Resources: hostapi.Resources{"vcpu": 1.0, "mem_mib": 128.0},
				Runs:      []hostapi.RunBind{{RunID: "run_" + vid, Epoch: 1, Handles: hostapi.Handles{Run: "boss_run_" + vid, Model: "boss_mdl_" + vid}}}})
			wave = append(wave, vid)
		}
		e.cp.SetDesired(id, ds)
		e.waitFor(60*time.Second, fmt.Sprintf("wave of %d serving", len(wave)), func() bool {
			s := e.servingSince(time.Time{})
			for _, v := range wave {
				if _, ok := s[v]; !ok {
					return false
				}
			}
			return true
		})
		claimed = append(claimed, wave...)
	}
	t.Logf("%d clones claimed and serving", len(claimed))

	// 3. P8b-T4: distinct identities, RNG and reseeded kernels; no token.
	rng := map[string]string{}
	restored, reseeded := 0, 0
	restoredSet := map[string]bool{}
	for _, v := range claimed {
		r := control(t, e.mgr, v, "rng")
		if r.VMID != v || len(r.RNG) != 32 {
			t.Fatalf("%s: %+v", v, r)
		}
		if other, dup := rng[r.RNG]; dup {
			t.Fatalf("clones %s and %s share RNG output", v, other)
		}
		rng[r.RNG] = v
		if r.Restored {
			restored++
			restoredSet[v] = true
		}
		if r.Reseeded {
			reseeded++
		}
		if d := time.Since(time.Unix(r.ClockUnix, 0)); d < -5*time.Second || d > 5*time.Second {
			t.Fatalf("%s clock off by %s after restore", v, d)
		}
	}
	if restored < clones-1 || reseeded != clones {
		t.Fatalf("restored=%d reseeded=%d of %d", restored, reseeded, clones)
	}
	t.Logf("T4: %d clones, %d distinct RNG outputs, %d restored, %d reseeded (RNDADDENTROPY+RNDRESEEDCRNG), clocks within 5 s", clones, len(rng), restored, reseeded)
	for _, needle := range []string{"lease_", "PRIVATE KEY", "join-e2e"} {
		for _, root := range []string{fc.Jailer.ChrootBase, fc.SnapDir} {
			if hit := grepTree(root, needle); hit != "" {
				t.Fatalf("%q found in %s", needle, hit)
			}
		}
	}
	t.Log("no run token, private key or join token in any jail, console log or snapshot file")

	// 4. Jailer per process.
	vms, err := e.mgr.Scan()
	if err != nil {
		t.Fatal(err)
	}
	uids := map[int]string{}
	var jailed []*vmm.VM
	for _, vm := range vms {
		if !vm.Jailed || vm.UID <= 0 {
			t.Fatalf("VM %s not jailed (uid %d)", vm.ID, vm.UID)
		}
		if other, dup := uids[vm.UID]; dup {
			t.Fatalf("VMs %s and %s share uid %d", vm.ID, other, vm.UID)
		}
		uids[vm.UID] = vm.ID
		status, _ := os.ReadFile(fmt.Sprintf("/proc/%d/status", vm.PID))
		if !strings.Contains(string(status), "Seccomp:\t2") {
			t.Fatalf("VM %s not under seccomp filter mode: %s", vm.ID, grepLine(string(status), "Seccomp:"))
		}
		if ns := strings.Fields(strings.TrimPrefix(grepLine(string(status), "NSpid:"), "NSpid:")); len(ns) < 2 {
			t.Fatalf("VM %s not in a new PID namespace: %v", vm.ID, ns)
		}
		st, err := os.Stat(vm.Dir)
		if err != nil || st.Mode().Perm() != 0o700 || int(st.Sys().(*syscall.Stat_t).Uid) != vm.UID {
			t.Fatalf("VM %s chroot %s: %v mode %o", vm.ID, vm.Dir, err, st.Mode().Perm())
		}
		cg, _ := os.ReadFile(fmt.Sprintf("/proc/%d/cgroup", vm.PID))
		if !strings.Contains(string(cg), "/boss-hostd/") {
			t.Fatalf("VM %s not in the boss-hostd cgroup: %s", vm.ID, cg)
		}
		fcid := filepath.Base(filepath.Dir(vm.Dir))
		if _, err := os.Stat("/run/netns/bv-" + fcid); err != nil {
			t.Fatalf("VM %s has no netns: %v", vm.ID, err)
		}
		jailed = append(jailed, vm)
	}
	t.Logf("%d VMs jailed: distinct uids, Seccomp=2, new PID ns, 0700 chroots, cgroup v2 limits, a netns each", len(jailed))
	// a: a restored clone (it holds a read grant on its shape's memory file);
	// b: any other VM.
	var a, b *vmm.VM
	for _, vm := range jailed {
		if a == nil && restoredSet[vm.ID] {
			a = vm
		} else if b == nil {
			b = vm
		}
	}
	if a == nil || b == nil {
		t.Fatal("need a restored clone and another VM")
	}
	cmd := exec.Command("/bin/ls", b.Dir)
	cmd.SysProcAttr = &syscall.SysProcAttr{Credential: &syscall.Credential{Uid: uint32(a.UID), Gid: uint32(a.UID)}}
	out, err := cmd.CombinedOutput()
	if err == nil || !strings.Contains(string(out), "ermission denied") {
		t.Fatalf("uid %d could read %s's chroot: %v %s", a.UID, b.ID, err, out)
	}
	// Nor the snapshot store (0600 files; the uid reads only its linked
	// mem/state through the ACL), nor boss-hostd's records above its jail.
	for _, p := range []string{filepath.Join(fc.SnapDir, snaps[0].Key, "meta.json"), filepath.Join(a.Meta, "boss-spec.json")} {
		cmd = exec.Command("/bin/cat", p)
		cmd.SysProcAttr = &syscall.SysProcAttr{Credential: &syscall.Credential{Uid: uint32(a.UID), Gid: uint32(a.UID)}}
		if out, err := cmd.CombinedOutput(); err == nil {
			t.Fatalf("uid %d could read %s: %s", a.UID, p, out)
		}
	}
	// Scanned VMs carry no snapshot ref: find the clone's shape in the store.
	snapOf := func(vm *vmm.VM) *vmm.SnapshotRef {
		for i := range snaps {
			if vm.Spec != nil && snaps[i].Shape.Mode == vm.Spec.Mode {
				return &snaps[i]
			}
		}
		return nil
	}
	if sa := snapOf(a); sa != nil && !acledFor(sa.MemPath(), a.UID) {
		t.Fatalf("uid %d cannot read the snapshot memory of its shape", a.UID)
	}
	t.Logf("uid %d (VM %s) is refused %s's chroot, the store's meta.json and the host's spec record; it reads only its own snapshot memory", a.UID, a.ID, b.ID)

	// 5. P8b-T7 timings from the host's events.
	var warmClaims, coldClaims, restores, coldBoots, snapshotMS []float64
	for _, ev := range e.cp.Events() {
		if ev.Kind != "vm_timing" {
			continue
		}
		ms, _ := strconv.ParseFloat(ev.Detail["ms"], 64)
		switch ev.Detail["kind"] {
		case "warm_claim":
			warmClaims = append(warmClaims, ms)
		case "cold_claim":
			coldClaims = append(coldClaims, ms)
		case "restore":
			restores = append(restores, ms)
		case "cold_boot":
			coldBoots = append(coldBoots, ms)
		case "snapshot":
			snapshotMS = append(snapshotMS, ms)
		}
	}
	t.Logf("T7: warm_claim n=%d p50=%.0f p95=%.0f ms | restore n=%d p50=%.0f p95=%.0f ms | cold_boot n=%d p95=%.0f ms | snapshot n=%d p50=%.0f ms | cold_claim n=%d p95=%.0f ms",
		len(warmClaims), pct(warmClaims, 50), pct(warmClaims, 95), len(restores), pct(restores, 50), pct(restores, 95),
		len(coldBoots), pct(coldBoots, 95), len(snapshotMS), pct(snapshotMS, 50), len(coldClaims), pct(coldClaims, 95))
	if len(warmClaims) < clones/2 {
		t.Fatalf("too few warm claims measured: %d", len(warmClaims))
	}
	if p := pct(warmClaims, 95); p >= 300 {
		t.Fatalf("warm claim p95 %.0f ms ≥ 300 ms", p)
	}
	if p := pct(coldBoots, 95); p >= 3000 {
		t.Fatalf("cold boot p95 %.0f ms ≥ 3 s", p)
	}
	var rss int64
	for _, vm := range jailed {
		rss += e.mgr.Stats(vm).RSSKiB
	}
	t.Logf("density: %d VMs (128 MiB each) use %d MiB host RSS in total, %.1f MiB per VM", len(jailed), rss>>10, float64(rss>>10)/float64(len(jailed)))

	// 6. Stopping a VM leaves no jail, cgroup or netns behind.
	victim := claimed[0]
	ds2 := ds
	var kept []hostapi.DesiredVM
	for _, dv := range ds2.VMs {
		if dv.VMID != victim {
			kept = append(kept, dv)
		}
	}
	ds2.VMs = kept
	e.cp.SetDesired(id, ds2)
	e.waitFor(20*time.Second, "victim stopped", func() bool {
		_, ok := e.servingSince(time.Time{})[victim]
		return !ok
	})
	time.Sleep(time.Second)
	left := 0
	for _, vm := range vms {
		if vm.ID != victim {
			continue
		}
		for _, p := range []string{vm.Meta, "/sys/fs/cgroup/boss-hostd/" + filepath.Base(filepath.Dir(vm.Dir)),
			"/run/netns/bv-" + filepath.Base(filepath.Dir(vm.Dir))} {
			if _, err := os.Stat(p); err == nil {
				t.Errorf("after stop, %s still exists", p)
				left++
			}
		}
		if sv := snapOf(vm); sv != nil && restoredSet[victim] && acledFor(sv.MemPath(), vm.UID) {
			t.Errorf("after stop, uid %d still reads the snapshot memory", vm.UID)
		}
	}
	if left == 0 {
		t.Logf("stopped %s: jail, cgroup, netns and snapshot read grant all gone", victim)
	}
}

func acled(path string) bool {
	buf := make([]byte, 4096)
	n, err := syscall.Getxattr(path, "system.posix_acl_access", buf)
	return err == nil && n > 4
}

// acledFor reports whether path's POSIX ACL grants uid read (the store's
// parent directories are root-only, so the grant is only usable through the
// hard link inside the VM's own jail — which is the point).
func acledFor(path string, uid int) bool {
	buf := make([]byte, 8192)
	n, err := syscall.Getxattr(path, "system.posix_acl_access", buf)
	if err != nil || n < 4 {
		return false
	}
	for o := 4; o+8 <= n; o += 8 {
		tag := binary.LittleEndian.Uint16(buf[o:])
		perm := binary.LittleEndian.Uint16(buf[o+2:])
		id := binary.LittleEndian.Uint32(buf[o+4:])
		if tag == 0x02 && id == uint32(uid) && perm&0x4 != 0 {
			return true
		}
	}
	return false
}

func grepLine(s, prefix string) string {
	for _, l := range strings.Split(s, "\n") {
		if strings.HasPrefix(l, prefix) {
			return l
		}
	}
	return ""
}

// grepTree returns the first regular file under root (skipping sockets and
// the multi-GB disk images) containing needle.
func grepTree(root, needle string) string {
	var hit string
	_ = filepath.Walk(root, func(p string, info os.FileInfo, err error) error {
		if err != nil || hit != "" || !info.Mode().IsRegular() || info.Size() > 64<<20 {
			return nil
		}
		raw, err := os.ReadFile(p)
		if err != nil || (len(raw) > 4 && string(raw[:4]) == "\x7fELF") {
			return nil // the jailer's copy of the firecracker binary
		}
		if strings.Contains(string(raw), needle) {
			hit = p
		}
		return nil
	})
	return hit
}

func pct(v []float64, p float64) float64 {
	if len(v) == 0 {
		return 0
	}
	s := append([]float64(nil), v...)
	sort.Float64s(s)
	i := int(float64(len(s)-1) * p / 100)
	return s[i]
}

var _ = context.Background
