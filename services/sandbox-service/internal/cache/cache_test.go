package cache

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
)

func TestCacheNeverStoresHandlesAndIsRebuildable(t *testing.T) {
	p := filepath.Join(t.TempDir(), "cache.db")
	c, err := Open(p)
	if err != nil {
		t.Fatal(err)
	}
	ds := hostapi.DesiredState{Generation: 4, VMs: []hostapi.DesiredVM{{VMID: "vm-1", ReplicaHandle: "boss_rep_SECRET",
		Runs: []hostapi.RunBind{{RunID: "r", Handles: hostapi.Handles{Model: "boss_mdl_SECRET"}}}}}}
	if err := c.PutDesired(ds); err != nil {
		t.Fatal(err)
	}
	if err := c.PutVM(VMRecord{VMID: "vm-1", PID: 42}); err != nil {
		t.Fatal(err)
	}
	c.Close()
	raw, _ := os.ReadFile(p)
	if strings.Contains(string(raw), "SECRET") {
		t.Fatal("a handle reached the bbolt file")
	}
	info, _ := os.Stat(p)
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("cache mode %v", info.Mode())
	}
	// Losing the file is fine: Open makes a fresh empty cache.
	os.Remove(p)
	c2, err := Open(p)
	if err != nil {
		t.Fatal(err)
	}
	defer c2.Close()
	vms, _ := c2.VMs()
	last, _ := c2.LastDesired()
	if len(vms) != 0 || last != nil {
		t.Fatal("a fresh cache must be empty")
	}
}
