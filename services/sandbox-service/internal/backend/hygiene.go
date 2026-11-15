package backend

import (
	"errors"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"strings"
)

// Task-directory hygiene (P8-pre S1/S2, BOSSAPP-1061).
//
// A task directory holds the guest spec (today it still carries the task's git
// and model credentials) and, after a run, the guest's repo. Failed and timed
// out tasks are retained for two hours and browsable over the fs endpoint, so:
//
//   - every file is owner-only (0600, directories 0700): no other host user can
//     read a task's credentials (defect 4);
//   - before a task is retained, the spec copies are deleted and every
//     .git/config is scrubbed of credentials in remote URLs (defects 3 and 5);
//   - the fs endpoint refuses spec.json, .git/config and *credentials* paths
//     even if one survives (IsSecretPath).

// FileMode and DirMode are the most a task-directory entry may grant.
const (
	FileMode fs.FileMode = 0o600
	DirMode  fs.FileMode = 0o700
)

// TightenTree makes every file under root owner-only (at most 0600) and every
// directory 0700. Symlinks are left alone (chmod would follow them out of the
// tree). Best effort: an entry that cannot be changed does not stop the walk.
func TightenTree(root string) error {
	return filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.Type()&fs.ModeSymlink != 0 {
			return nil
		}
		info, ierr := d.Info()
		if ierr != nil {
			return nil
		}
		if d.IsDir() {
			if info.Mode().Perm() != DirMode {
				_ = os.Chmod(p, DirMode)
			}
			return nil
		}
		if perm := info.Mode().Perm(); perm&^FileMode != 0 {
			_ = os.Chmod(p, perm&FileMode)
		}
		return nil
	})
}

// credentialInURL matches the userinfo of an http(s) URL: scheme://user:secret@
var credentialInURL = regexp.MustCompile(`(?i)\b(https?://)[^/@\s"']+@`)

// StripURLCredentials removes any userinfo (user:token@) from http(s) URLs in s.
func StripURLCredentials(s string) string {
	return credentialInURL.ReplaceAllString(s, "$1")
}

// extraHeader matches an http.extraheader line (an Authorization header git
// sends on every request) in any [http] section.
var extraHeader = regexp.MustCompile(`(?im)^[ \t]*extraheader[ \t]*=.*(\r?\n|$)`)

// ScrubGitConfig removes credentials from a git config: userinfo in URLs and
// every http.extraheader line.
func ScrubGitConfig(s string) string {
	return extraHeader.ReplaceAllString(StripURLCredentials(s), "")
}

func underGitDir(root, p string) bool {
	rel, err := filepath.Rel(root, p)
	if err != nil {
		return false
	}
	for _, part := range strings.Split(filepath.ToSlash(rel), "/") {
		if part == ".git" {
			return true
		}
	}
	return false
}

// ScrubRetained prepares a finished task's directory to be kept: the guest spec
// copies are deleted (they carry the task's credentials) and every .git/config
// in the retained tree has credentials removed from its remote URLs.
func ScrubRetained(taskDir string) {
	if taskDir == "" {
		return
	}
	for _, rel := range []string{"spec.json", filepath.Join("workstage", "spec.json")} {
		_ = os.Remove(filepath.Join(taskDir, rel))
	}
	_ = filepath.WalkDir(taskDir, func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() || d.Type()&fs.ModeSymlink != 0 {
			return nil
		}
		if d.Name() == "spec.json" {
			_ = os.Remove(p)
			return nil
		}
		if d.Name() == "config" && underGitDir(taskDir, p) {
			raw, rerr := os.ReadFile(p)
			if rerr != nil {
				return nil
			}
			if clean := ScrubGitConfig(string(raw)); clean != string(raw) {
				_ = os.WriteFile(p, []byte(clean), FileMode)
			}
		}
		return nil
	})
}

// ErrOutsideRoot: a path that resolves (through symlinks) outside its root.
var ErrOutsideRoot = errors.New("path resolves outside the retained workspace")

// ResolveWithin resolves every symlink in p and requires the result to stay
// inside root (itself resolved). It returns the resolved path and its
// slash-separated path relative to root ("" for root itself).
func ResolveWithin(root, p string) (string, string, error) {
	realRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		return "", "", err
	}
	real, err := filepath.EvalSymlinks(p)
	if err != nil {
		return "", "", err
	}
	rel, err := filepath.Rel(realRoot, real)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) ||
		filepath.IsAbs(rel) {
		return "", "", ErrOutsideRoot
	}
	if rel == "." {
		rel = ""
	}
	return real, filepath.ToSlash(rel), nil
}

// IsSecretPath reports whether a retained-workspace path (slash-separated,
// relative or absolute) names something the fs endpoint must never serve:
// a guest spec, a git config, or anything named like a credentials file.
func IsSecretPath(p string) bool {
	clean := path.Clean("/" + strings.ReplaceAll(p, "\\", "/"))
	parts := strings.Split(strings.ToLower(strings.Trim(clean, "/")), "/")
	base := parts[len(parts)-1]
	if base == "spec.json" {
		return true
	}
	inGit := false
	for i, part := range parts {
		// A credentials file, or anything under a credentials/ directory.
		if strings.Contains(part, "credentials") {
			return true
		}
		if part == ".git" && i < len(parts)-1 {
			inGit = true
		}
	}
	// .git/config, .git/modules/<m>/config, .git/worktrees/<w>/config, ...
	return inGit && base == "config"
}
