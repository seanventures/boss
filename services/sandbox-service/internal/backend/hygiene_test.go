package backend

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// P8pre-T1 (unit half): the Firecracker staging writes the spec and the VM
// config owner-only — they were 0644, readable by every host user.
func TestStagedSpecAndVMConfigAreOwnerOnly(t *testing.T) {
	dir := t.TempDir()
	specPath := filepath.Join(dir, "spec.json")
	if err := os.WriteFile(specPath, []byte(`{"token":"x"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	stage, err := stageSpec(dir, specPath)
	if err != nil {
		t.Fatal(err)
	}
	assertMode(t, filepath.Join(stage, "spec.json"), 0o600)
	assertMode(t, stage, 0o700)

	cfgPath := filepath.Join(dir, "vm.json")
	if err := os.WriteFile(cfgPath, []byte("{}"), 0o644); err != nil { // a wider leftover
		t.Fatal(err)
	}
	if err := writeVMConfig(cfgPath, map[string]any{"machine-config": map[string]any{}}); err != nil {
		t.Fatal(err)
	}
	assertMode(t, cfgPath, 0o600)
}

func TestTightenTreeLeavesNothingWiderThanOwnerOnly(t *testing.T) {
	root := t.TempDir()
	sub := filepath.Join(root, "work", "repo")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	for name, mode := range map[string]os.FileMode{"a.txt": 0o644, "run.sh": 0o755, "k": 0o400} {
		if err := os.WriteFile(filepath.Join(sub, name), []byte("x"), mode); err != nil {
			t.Fatal(err)
		}
	}
	if err := TightenTree(root); err != nil {
		t.Fatal(err)
	}
	assertTreeOwnerOnly(t, root)
	assertMode(t, filepath.Join(sub, "k"), 0o400) // already tighter: left as is
}

// P8pre-T2 (unit half): a retained workspace keeps no spec and no token.
func TestScrubRetainedDropsSpecsAndCleansGitConfig(t *testing.T) {
	root := t.TempDir()
	gitDir := filepath.Join(root, "work", "repo", ".git")
	if err := os.MkdirAll(gitDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "workstage"), 0o755); err != nil {
		t.Fatal(err)
	}
	secret := "ghs_" + strings.Repeat("s", 36)
	for _, p := range []string{"spec.json", "workstage/spec.json", "work/spec.json"} {
		if err := os.WriteFile(filepath.Join(root, p), []byte(secret), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	cfg := "[remote \"origin\"]\n\turl = https://x-access-token:" + secret + "@github.com/o/r.git\n" +
		"[remote \"bb\"]\n\turl = https://x-token-auth:" + secret + "@bitbucket.org/o/r.git\n"
	if err := os.WriteFile(filepath.Join(gitDir, "config"), []byte(cfg), 0o644); err != nil {
		t.Fatal(err)
	}
	ScrubRetained(root)
	for _, p := range []string{"spec.json", "workstage/spec.json", "work/spec.json"} {
		if _, err := os.Stat(filepath.Join(root, p)); !os.IsNotExist(err) {
			t.Fatalf("%s must be deleted before retention", p)
		}
	}
	raw, err := os.ReadFile(filepath.Join(gitDir, "config"))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), secret) || strings.Contains(string(raw), "@github.com") {
		t.Fatal(".git/config still carries a credential")
	}
	if !strings.Contains(string(raw), "https://github.com/o/r.git") ||
		!strings.Contains(string(raw), "https://bitbucket.org/o/r.git") {
		t.Fatalf("remote URLs must survive, credential-free: %q", raw)
	}
}

func TestIsSecretPathNamesTheThreeRefusedShapes(t *testing.T) {
	for _, p := range []string{"work/spec.json", "/work/repo/.git/config", "work/repo/.git/../.git/config",
		"work/repo/.credentials.json", "work/repo/aws_credentials", "work/repo/Credentials.txt"} {
		if !IsSecretPath(p) {
			t.Fatalf("%s must be refused", p)
		}
	}
	for _, p := range []string{"work/repo", "work/repo/README.md", "work/repo/.git/HEAD",
		"work/repo/config", "work/agent.log"} {
		if IsSecretPath(p) {
			t.Fatalf("%s must stay browsable", p)
		}
	}
}

func assertMode(t *testing.T, p string, want os.FileMode) {
	t.Helper()
	info, err := os.Stat(p)
	if err != nil {
		t.Fatal(err)
	}
	if got := info.Mode().Perm(); got != want {
		t.Fatalf("%s mode %o, want %o", p, got, want)
	}
}

func assertTreeOwnerOnly(t *testing.T, root string) {
	t.Helper()
	_ = filepath.Walk(root, func(p string, info os.FileInfo, err error) error {
		if err != nil {
			t.Fatal(err)
		}
		limit := FileMode
		if info.IsDir() {
			limit = DirMode
		}
		if info.Mode().Perm()&^limit != 0 {
			t.Fatalf("%s is %o, wider than %o", p, info.Mode().Perm(), limit)
		}
		return nil
	})
}
