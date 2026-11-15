package vmm

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"time"
)

// Store is the on-disk snapshot store: <Dir>/<key>/{meta.json, mem, state,
// overlay.ext4}. Everything is written 0600 (LLD §3.4 S15 rule 5); the
// Firecracker backend grants read access to a restoring VM's uid with a POSIX
// ACL and revokes it when that VM stops (acl_linux.go).
type Store struct{ Dir string }

const metaFile = "meta.json"

func (s Store) dir(key string) string { return filepath.Join(s.Dir, key) }

// Find returns the stored snapshot for a shape.
func (s Store) Find(shape Shape) (SnapshotRef, bool) {
	ref, err := s.read(s.dir(shape.Key()))
	if err != nil {
		return SnapshotRef{}, false
	}
	return ref, true
}

func (s Store) read(dir string) (SnapshotRef, error) {
	raw, err := os.ReadFile(filepath.Join(dir, metaFile))
	if err != nil {
		return SnapshotRef{}, err
	}
	var ref SnapshotRef
	if err := json.Unmarshal(raw, &ref); err != nil {
		return SnapshotRef{}, err
	}
	ref.Dir = dir
	return ref, nil
}

// List returns every stored snapshot, oldest first.
func (s Store) List() []SnapshotRef {
	ents, err := os.ReadDir(s.Dir)
	if err != nil {
		return nil
	}
	var out []SnapshotRef
	for _, e := range ents {
		if !e.IsDir() {
			continue
		}
		if ref, err := s.read(filepath.Join(s.Dir, e.Name())); err == nil {
			out = append(out, ref)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.Before(out[j].CreatedAt) })
	return out
}

// Begin creates the snapshot directory (0700) for a shape, failing if a
// complete snapshot already exists. A half-written one from a crash is
// replaced.
func (s Store) Begin(shape Shape) (string, error) {
	dir := s.dir(shape.Key())
	if _, err := os.Stat(filepath.Join(dir, metaFile)); err == nil {
		return "", fmt.Errorf("snapshot %s already exists", shape.Key())
	}
	_ = os.RemoveAll(dir)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	return dir, nil
}

// Commit writes meta.json last, so a snapshot is visible only once complete.
func (s Store) Commit(ref SnapshotRef) error {
	for _, name := range []string{"mem", "state", "overlay.ext4"} {
		p := filepath.Join(ref.Dir, name)
		if _, err := os.Stat(p); err == nil {
			_ = os.Chmod(p, 0o600)
		}
	}
	raw, err := json.Marshal(ref)
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(ref.Dir, metaFile), raw, 0o600)
}

// Delete removes a snapshot and its files.
func (s Store) Delete(ref SnapshotRef) error {
	if ref.Dir == "" || s.Dir == "" || filepath.Dir(ref.Dir) != filepath.Clean(s.Dir) {
		return errors.New("snapshot dir is not inside the store")
	}
	return os.RemoveAll(ref.Dir)
}

// MemPath, StatePath and OverlayPath name a snapshot's files.
func (r SnapshotRef) MemPath() string     { return filepath.Join(r.Dir, "mem") }
func (r SnapshotRef) StatePath() string   { return filepath.Join(r.Dir, "state") }
func (r SnapshotRef) OverlayPath() string { return filepath.Join(r.Dir, "overlay.ext4") }

// copySparse copies src to dst (mode 0600), turning zero blocks into holes so
// an overlay or memory image costs only its written pages.
func copySparse(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	st, err := in.Stat()
	if err != nil {
		return err
	}
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	defer out.Close()
	buf := make([]byte, 1<<20)
	var off int64
	for {
		n, err := io.ReadFull(in, buf)
		if n > 0 {
			if !allZero(buf[:n]) {
				if _, werr := out.WriteAt(buf[:n], off); werr != nil {
					return werr
				}
			}
			off += int64(n)
		}
		if err == io.EOF || err == io.ErrUnexpectedEOF {
			break
		}
		if err != nil {
			return err
		}
	}
	return out.Truncate(st.Size())
}

func allZero(b []byte) bool {
	for _, c := range b {
		if c != 0 {
			return false
		}
	}
	return true
}

// moveFile renames when possible (same filesystem) and copies otherwise.
func moveFile(src, dst string) error {
	if err := os.Rename(src, dst); err == nil {
		return nil
	}
	if err := copySparse(src, dst); err != nil {
		return err
	}
	return os.Remove(src)
}

// snapshotAge is the time since the snapshot was taken.
func (r SnapshotRef) Age(now time.Time) time.Duration { return now.Sub(r.CreatedAt) }
