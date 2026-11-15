package vmm

import (
	"encoding/json"
	"strings"
	"testing"
)

// LLD §3.7.3: a per_run VM is configured with no network-interfaces entry at
// all; a replica gets exactly one tap. Both share the read-only base rootfs.
func TestFirecrackerConfigNICOnlyForReplicas(t *testing.T) {
	f := &Firecracker{Kernel: "/k", Rootfs: "/base.ext4"}
	perRun := f.VMConfig(Spec{VMID: "run-1", Mode: "per_run"}, "/d")
	raw, _ := json.Marshal(perRun)
	if strings.Contains(string(raw), "network-interfaces") {
		t.Fatalf("per_run VM has a NIC: %s", raw)
	}
	rep := f.VMConfig(Spec{VMID: "vm-1", Mode: "replica", Net: &NetAttach{Tap: "bt0123456789ab", MAC: "02:fc:0a:c8:04:07"}}, "/d")
	nics := rep["network-interfaces"].([]map[string]any)
	if len(nics) != 1 || nics[0]["host_dev_name"] != "bt0123456789ab" {
		t.Fatalf("replica NIC: %+v", nics)
	}
	for _, cfg := range []map[string]any{perRun, rep} {
		d := cfg["drives"].([]map[string]any)[0]
		if d["is_read_only"] != true || d["path_on_host"] != "/base.ext4" {
			t.Fatalf("rootfs must be the shared read-only base: %+v", d)
		}
		if !strings.Contains(cfg["boot-source"].(map[string]any)["boot_args"].(string), "init=/sbin/boss-vsockd") {
			t.Fatal("init must be boss-vsockd")
		}
		if cfg["vsock"] == nil {
			t.Fatal("vsock device missing")
		}
	}
}

func TestVMIDPattern(t *testing.T) {
	for _, ok := range []string{"vm-1", "rep-0a1b2c", "A9", "vm_0123456789abcdef", "x_y"} {
		if !VMIDPattern.MatchString(ok) {
			t.Fatal(ok)
		}
	}
	for _, bad := range []string{"", "-x", "_x", "../etc", "a b", "a/b", strings.Repeat("a", 49)} {
		if VMIDPattern.MatchString(bad) {
			t.Fatal(bad)
		}
	}
	if FirecrackerID("vm_0123abcd") != "vm-0123abcd" {
		t.Fatal("firecracker --id must map _ to -")
	}
}

func TestSnapshotSeamsSayP8b(t *testing.T) {
	for _, m := range []Manager{&Process{}, &Firecracker{}} {
		if _, err := m.Restore(nil, Spec{}, SnapshotRef{}); err != ErrNotImplemented {
			t.Fatal(err)
		}
	}
}
