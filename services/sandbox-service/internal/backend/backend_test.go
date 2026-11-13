package backend

import (
	"bytes"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// The embedded guest agent must stay byte-identical to the canonical copy in
// sandbox-host/guest/in_guest_agent.py — one pipeline, two launch paths.
func TestEmbeddedGuestAgentMatchesSandboxHostCopy(t *testing.T) {
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("caller")
	}
	canonical := filepath.Join(filepath.Dir(file), "..", "..", "..", "..",
		"sandbox-host", "guest", "in_guest_agent.py")
	want, err := os.ReadFile(canonical)
	if err != nil {
		t.Skipf("canonical guest agent not present (standalone build?): %v", err)
	}
	if !bytes.Equal(want, EmbeddedGuestAgent) {
		t.Fatal("services/sandbox-service/internal/backend/guest/in_guest_agent.py drifted from " +
			"sandbox-host/guest/in_guest_agent.py — re-copy it (make sandbox-guest-sync)")
	}
}

func TestProcessCheckNamesTheMissingPiece(t *testing.T) {
	p := &Process{Python: "definitely-not-a-python-binary"}
	err := p.Check()
	if err == nil {
		t.Fatal("expected a readiness error")
	}
	if !strings.Contains(err.Error(), "definitely-not-a-python-binary") {
		t.Fatalf("error must name the missing piece: %v", err)
	}
}

// Every firecracker readiness failure must say exactly which artifact is
// missing — the message is what the 422 rejection and health reason carry.
func TestFirecrackerCheckFailsClosedNamingEachMissingPiece(t *testing.T) {
	if runtime.GOOS != "linux" {
		f := &Firecracker{}
		err := f.Check()
		if err == nil || !strings.Contains(err.Error(), "requires Linux") {
			t.Fatalf("non-linux must refuse cleanly: %v", err)
		}
		return
	}
	dir := t.TempDir()
	touch := func(name string) string {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte("x"), 0o755); err != nil {
			t.Fatal(err)
		}
		return p
	}
	cases := []struct {
		name string
		f    Firecracker
		want string
	}{
		{"no binary", Firecracker{}, "BOSS_SANDBOX_FC_BIN"},
		{"no kernel", Firecracker{Bin: touch("firecracker")}, "BOSS_SANDBOX_FC_KERNEL"},
		{"no rootfs", Firecracker{Bin: touch("firecracker"), Kernel: touch("vmlinux")},
			"BOSS_SANDBOX_FC_ROOTFS"},
	}
	for _, tc := range cases {
		err := tc.f.Check()
		if err == nil {
			// /dev/kvm may be missing first on CI hosts — that is also a named refusal.
			t.Fatalf("%s: expected error", tc.name)
		}
		if !strings.Contains(err.Error(), tc.want) && !strings.Contains(err.Error(), "/dev/kvm") {
			t.Fatalf("%s: error must name the missing piece, got: %v", tc.name, err)
		}
	}
}
