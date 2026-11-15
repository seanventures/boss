package vmm

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

// LLD §3.6: disk 50 MB/s and 1000 IOPS on every drive, 100 Mbit/s on the
// replica tap; §3.4: a shared read-only base plus a per-VM overlay; §3.18:
// MMDS V2 for replicas only.
func TestFirecrackerConfigLimitsOverlayNoMMDS(t *testing.T) {
	f := &Firecracker{Kernel: "/k", Rootfs: "/base.ext4", OverlayMiB: 1024}
	rep := f.VMConfig(Spec{VMID: "vm-1", Mode: "replica", Net: &NetAttach{Tap: "bt0123456789ab", MAC: "02:fc:0a:c8:04:07"}}, "/d")
	drives := rep["drives"].([]map[string]any)
	if len(drives) != 2 || drives[0]["is_read_only"] != true || drives[1]["drive_id"] != "overlay" || drives[1]["is_read_only"] != false {
		t.Fatalf("drives: %+v", drives)
	}
	for _, d := range drives {
		rl := d["rate_limiter"].(map[string]any)
		if rl["bandwidth"].(map[string]any)["size"] != DiskBytesPerSec || rl["ops"].(map[string]any)["size"] != DiskIOPS {
			t.Fatalf("disk limiter: %+v", rl)
		}
	}
	nic := rep["network-interfaces"].([]map[string]any)[0]
	for _, k := range []string{"rx_rate_limiter", "tx_rate_limiter"} {
		if nic[k].(map[string]any)["bandwidth"].(map[string]any)["size"] != NetBytesPerSec {
			t.Fatalf("%s: %+v", k, nic[k])
		}
	}
	if rep["mmds-config"] != nil {
		t.Fatal("no MMDS until a consumer and the in-guest root-only rule exist")
	}
	perRun := f.VMConfig(Spec{VMID: "run-1", Mode: "per_run"}, "/d")
	if perRun["mmds-config"] != nil || perRun["network-interfaces"] != nil {
		t.Fatalf("per_run VM has a NIC or MMDS: %+v", perRun)
	}
	// Jailed: paths are chroot-relative and the guest tap name is uniform.
	jf := &Firecracker{Kernel: "/k", Rootfs: "/base.ext4", OverlayMiB: 1024, Jailer: &Jailer{Bin: "/j", ChrootBase: "/srv/j"}}
	l := jf.layoutFor("vm_1")
	jc := jf.config(Spec{VMID: "vm_1", Mode: "replica", Net: &NetAttach{Tap: "bt0123456789ab"}}, l)
	if jc["drives"].([]map[string]any)[0]["path_on_host"] != "/rootfs.ext4" || jc["vsock"].(map[string]any)["uds_path"] != "/v.sock" {
		t.Fatalf("jailed paths must be chroot-relative: %+v", jc)
	}
	if jc["network-interfaces"].([]map[string]any)[0]["host_dev_name"] != GuestTap {
		t.Fatal("a jailed replica's NIC must be the uniform in-netns tap")
	}
	if l.host != "/srv/j/firecracker/vm-1/root" || l.apiSock() != "/srv/j/firecracker/vm-1/root/run/firecracker.socket" {
		t.Fatalf("layout: %+v", l)
	}
}

// LLD §3.18: unique uid/gid per VM, cgroup v2 cpu.max/memory.max = VM + 32 MiB,
// new PID namespace, netns, resource limits; default seccomp (no flag ever);
// API-socket mode (the jailer adds --id and --api-sock itself).
func TestJailerArgv(t *testing.T) {
	j := &Jailer{Bin: "/usr/bin/jailer", ChrootBase: "/srv/j"}
	argv := j.argv("vm-1", "/opt/firecracker", 100042, 2, 256, "/run/netns/bv-vm-1", "--config-file", "/vm.json")
	joined := strings.Join(argv, " ")
	for _, want := range []string{"--uid 100042", "--gid 100042", "--cgroup-version 2", "--cgroup cpu.max=200000 100000",
		"--cgroup memory.max=301989888", "--new-pid-ns", "--netns /run/netns/bv-vm-1", "--chroot-base-dir /srv/j",
		"--resource-limit fsize=", "--resource-limit no-file=4096", "-- --config-file /vm.json", "--exec-file /opt/firecracker"} {
		if !strings.Contains(joined, want) {
			t.Fatalf("jailer argv lacks %q: %s", want, joined)
		}
	}
	for _, never := range []string{"--no-seccomp", "--seccomp-filter", "--no-api"} {
		if strings.Contains(joined, never) {
			t.Fatalf("jailer argv must never carry %s", never)
		}
	}
	after := strings.SplitN(joined, " -- ", 2)[1]
	if strings.Contains(after, "--id") || strings.Contains(after, "--api-sock") {
		t.Fatalf("the jailer adds --id and --api-sock itself: %s", after)
	}
	// uids are stable and distinct across VMs.
	a, b := j.uidFor("vm-1"), j.uidFor("vm-2")
	if a == j.uidFor("vm-1") && a == b {
		t.Fatalf("uids collide: %d %d", a, b)
	}
	if a < j.UIDBase || a >= j.UIDBase+j.UIDRange {
		t.Fatalf("uid %d outside the pool", a)
	}
}

// An unjailed replica is never snapshotted: Firecracker 1.10 restores the
// snapshot's tap name, so a clone would open the template's tap.
func TestUnjailedReplicaSnapshotRefused(t *testing.T) {
	f := &Firecracker{Kernel: "/k", Rootfs: "/r", SnapDir: t.TempDir()}
	vm := &VM{ID: "vm-1", Spec: &Spec{VMID: "vm-1", Mode: "replica", ImageDigest: "sha256:a", Net: &NetAttach{Tap: "bt0123456789ab"}}}
	if _, err := f.Snapshot(context.Background(), vm); !errors.Is(err, ErrReplicaNeedsJail) {
		t.Fatalf("want ErrReplicaNeedsJail, got %v", err)
	}
}

// Parallel fills (warm pool) must never share a uid: the live set is the
// reservation, released at stop.
func TestJailerUIDsDistinctUnderParallelFills(t *testing.T) {
	j := &Jailer{Bin: "/usr/bin/jailer", ChrootBase: t.TempDir(), UIDBase: 5000, UIDRange: 64}
	var mu sync.Mutex
	seen := map[int]string{}
	var wg sync.WaitGroup
	for i := 0; i < 48; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			id := fmt.Sprintf("w-%02d", i)
			uid := j.uidFor(id)
			mu.Lock()
			defer mu.Unlock()
			if other, dup := seen[uid]; dup {
				t.Errorf("uid %d given to %s and %s", uid, other, id)
			}
			seen[uid] = id
		}(i)
	}
	wg.Wait()
	if len(seen) != 48 {
		t.Fatalf("want 48 distinct uids, got %d", len(seen))
	}
	for uid := range seen {
		j.release(uid)
	}
	if again := j.uidFor("w-00"); again < j.UIDBase || again >= j.UIDBase+j.UIDRange {
		t.Fatalf("after release: %d", again)
	}
}

// Check names its reasons (heartbeat facts.reasons): a missing binary is
// jailer_binary_missing before anything else.
func TestJailerCheckNamesReasons(t *testing.T) {
	err := (&Jailer{Bin: "/nonexistent/jailer", ChrootBase: "/srv/j"}).Check()
	if err == nil || !strings.Contains(err.Error(), ReasonJailerMissing) {
		t.Fatalf("want %s, got %v", ReasonJailerMissing, err)
	}
	f := &Firecracker{Jailer: &Jailer{Bin: "/nonexistent/jailer"}}
	if on, why := f.JailerOn(); on || why == "" {
		t.Fatalf("jailer_on must be false with a reason: %v %q", on, why)
	}
	if on, why := (&Firecracker{}).JailerOn(); on || !strings.Contains(why, "not configured") {
		t.Fatalf("no jailer: %v %q", on, why)
	}
}

// LLD §3.4: the snapshot key is (image_digest, kernel_digest, vcpu, mem_mib,
// runtime_version) — plus the mode, since a per_run snapshot has no NIC.
func TestShapeKey(t *testing.T) {
	a := Spec{ImageDigest: "sha256:a", KernelDigest: "sha256:k", VCPU: 1, MemMiB: 256, RuntimeVersion: "1.0", Mode: "per_run"}
	if a.Shape().Key() != a.Shape().Key() || len(a.Shape().Key()) != 24 {
		t.Fatal("key must be stable")
	}
	for _, change := range []func(s *Spec){
		func(s *Spec) { s.ImageDigest = "sha256:b" }, func(s *Spec) { s.KernelDigest = "sha256:k2" },
		func(s *Spec) { s.VCPU = 2 }, func(s *Spec) { s.MemMiB = 512 }, func(s *Spec) { s.RuntimeVersion = "1.1" },
		func(s *Spec) { s.Mode = "replica" },
	} {
		b := a
		change(&b)
		if b.Shape().Key() == a.Shape().Key() {
			t.Fatalf("key ignores a field: %+v", b)
		}
	}
	if (Spec{ImageDigest: "x"}).Shape() != (Spec{ImageDigest: "x", VCPU: 1, MemMiB: 256}).Shape() {
		t.Fatal("defaults must normalise into the key")
	}
}

// S15 rule 1: a VM that received an identity, handle or run is never
// snapshotted; a clean snapshot records clean_attested and is found by shape.
func TestSnapshotHygieneGateAndStore(t *testing.T) {
	root := t.TempDir()
	p := &Process{Root: root, RuntimeVersion: "rt1"}
	spec := Spec{VMID: "w-1", Mode: "per_run", ImageDigest: "sha256:img", VCPU: 1, MemMiB: 128}
	clean := &VM{ID: "w-1", Spec: &spec}
	ref, err := p.Snapshot(context.Background(), clean)
	if err != nil || !ref.CleanAttested || ref.Shape.RuntimeVersion != "rt1" || ref.Shape.KernelDigest != "process" {
		t.Fatalf("clean snapshot: %v %+v", err, ref)
	}
	if got, ok := p.FindSnapshot(spec.Shape()); !ok || got.Key != ref.Key || got.Dir != ref.Dir {
		t.Fatalf("find: %v %+v", ok, got)
	}
	if _, ok := p.FindSnapshot(Spec{VMID: "x", Mode: "replica", ImageDigest: "sha256:img", VCPU: 1, MemMiB: 128}.Shape()); ok {
		t.Fatal("a replica shape must not find the per_run snapshot")
	}
	if _, err := p.Snapshot(context.Background(), clean); err == nil {
		t.Fatal("a second snapshot of the same shape must be refused")
	}
	tainted := &VM{ID: "w-2", Spec: &Spec{VMID: "w-2", Mode: "per_run", ImageDigest: "sha256:other"}}
	tainted.Taint("identity")
	if _, err := p.Snapshot(context.Background(), tainted); !errors.Is(err, ErrTainted) {
		t.Fatalf("tainted VM must be refused with ErrTainted, got %v", err)
	}
	if t2, why := tainted.Tainted(); !t2 || why != "identity" {
		t.Fatal("taint reason must be kept (the first one)")
	}
	st, _ := os.Stat(filepath.Join(ref.Dir, metaFile))
	if st.Mode().Perm() != 0o600 {
		t.Fatalf("meta must be 0600, is %o", st.Mode().Perm())
	}
	if list := p.Snapshots(); len(list) != 1 {
		t.Fatalf("list: %+v", list)
	}
	if err := p.DeleteSnapshot(ref); err != nil {
		t.Fatal(err)
	}
	if _, ok := p.FindSnapshot(spec.Shape()); ok {
		t.Fatal("deleted snapshot still found")
	}
	if err := p.DeleteSnapshot(SnapshotRef{Dir: "/etc"}); err == nil {
		t.Fatal("delete outside the store must be refused")
	}
}

// A claimed warm VM carries its control-plane identity in the spec record,
// and Differs compares the record with the desired spec.
func TestWarmClaimRewritesSpecRecord(t *testing.T) {
	dir := t.TempDir()
	warm := Spec{VMID: "w-abc", Mode: "per_run", ImageDigest: "sha256:img", Warm: true}
	if err := writeSpec(dir, warm); err != nil {
		t.Fatal(err)
	}
	vm := &VM{ID: "w-abc", Dir: dir, Meta: dir, Spec: &warm}
	claimed := warm
	claimed.VMID = "vm_0123456789abcdef"
	vm.Claim(claimed)
	rec := readSpec(dir)
	if rec == nil || rec.VMID != "vm_0123456789abcdef" || rec.Warm {
		t.Fatalf("record after claim: %+v", rec)
	}
	if d := Differs(rec, claimed); d != "" {
		t.Fatalf("claimed record differs from desired: %s", d)
	}
	raw, _ := json.Marshal(rec)
	if bytes.Contains(raw, []byte(`"warm":true`)) {
		t.Fatal("claimed VM still marked warm")
	}
}

// The overlay and memory copies keep holes: a mostly-empty image costs only
// its written blocks.
func TestCopySparseKeepsContent(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "src")
	f, err := os.Create(src)
	if err != nil {
		t.Fatal(err)
	}
	if err := f.Truncate(8 << 20); err != nil {
		t.Fatal(err)
	}
	payload := bytes.Repeat([]byte("boss"), 1024)
	if _, err := f.WriteAt(payload, 3<<20); err != nil {
		t.Fatal(err)
	}
	f.Close()
	dst := filepath.Join(dir, "dst")
	if err := copySparseFast(src, dst); err != nil {
		t.Fatal(err)
	}
	a, _ := os.ReadFile(src)
	b, _ := os.ReadFile(dst)
	if !bytes.Equal(a, b) {
		t.Fatal("copy differs")
	}
	st, _ := os.Stat(dst)
	if st.Mode().Perm() != 0o600 {
		t.Fatalf("copy must be 0600, is %o", st.Mode().Perm())
	}
}
