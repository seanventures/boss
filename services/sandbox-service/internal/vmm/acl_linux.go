//go:build linux

package vmm

import (
	"encoding/binary"
	"errors"
	"fmt"
	"os"
	"sort"
	"sync"

	"golang.org/x/sys/unix"
)

// aclMu serializes the read-modify-write of an ACL: parallel fills grant on
// the same shared inode, and a lost update would leave a VM with EACCES at
// /snapshot/load.
var aclMu sync.Mutex

// POSIX ACLs let a 0600 snapshot file be read by exactly the jailer uids of
// the VMs restored from it (LLD §3.4 S15 rule 5: files are 0600 to the owner;
// clones map the same inode MAP_PRIVATE so clean pages stay shared). The
// system.posix_acl_access xattr is written directly: no setfacl dependency.

const (
	aclVersion  = 0x0002
	aclUserObj  = 0x01
	aclUser     = 0x02
	aclGroupObj = 0x04
	aclGroup    = 0x08
	aclMask     = 0x10
	aclOther    = 0x20
	aclUndef    = 0xffffffff
	aclRead     = 0x4
	aclXattr    = "system.posix_acl_access"
)

type aclEntry struct {
	tag  uint16
	perm uint16
	id   uint32
}

func encodeACL(entries []aclEntry) []byte {
	sort.Slice(entries, func(i, j int) bool {
		if entries[i].tag != entries[j].tag {
			return entries[i].tag < entries[j].tag
		}
		return entries[i].id < entries[j].id
	})
	out := make([]byte, 4+8*len(entries))
	binary.LittleEndian.PutUint32(out, aclVersion)
	for i, e := range entries {
		o := 4 + 8*i
		binary.LittleEndian.PutUint16(out[o:], e.tag)
		binary.LittleEndian.PutUint16(out[o+2:], e.perm)
		binary.LittleEndian.PutUint32(out[o+4:], e.id)
	}
	return out
}

func decodeACL(raw []byte) ([]aclEntry, error) {
	if len(raw) < 4 || binary.LittleEndian.Uint32(raw) != aclVersion || (len(raw)-4)%8 != 0 {
		return nil, errors.New("malformed posix acl")
	}
	var out []aclEntry
	for o := 4; o+8 <= len(raw); o += 8 {
		out = append(out, aclEntry{tag: binary.LittleEndian.Uint16(raw[o:]),
			perm: binary.LittleEndian.Uint16(raw[o+2:]), id: binary.LittleEndian.Uint32(raw[o+4:])})
	}
	return out, nil
}

// readACL returns the file's access ACL, synthesised from the mode bits when
// it has none.
func readACL(path string) ([]aclEntry, error) {
	buf := make([]byte, 4096)
	n, err := unix.Getxattr(path, aclXattr, buf)
	if err == nil {
		return decodeACL(buf[:n])
	}
	if !errors.Is(err, unix.ENODATA) {
		return nil, err
	}
	st, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	m := uint16(st.Mode().Perm())
	return []aclEntry{
		{tag: aclUserObj, perm: (m >> 6) & 7, id: aclUndef},
		{tag: aclGroupObj, perm: (m >> 3) & 7, id: aclUndef},
		{tag: aclOther, perm: m & 7, id: aclUndef},
	}, nil
}

func writeACL(path string, entries []aclEntry) error {
	// The mask covers every named entry (the group class); recompute it.
	var mask uint16
	var out []aclEntry
	for _, e := range entries {
		if e.tag == aclMask {
			continue
		}
		if e.tag == aclUser || e.tag == aclGroup || e.tag == aclGroupObj {
			mask |= e.perm
		}
		out = append(out, e)
	}
	hasNamed := false
	for _, e := range out {
		if e.tag == aclUser || e.tag == aclGroup {
			hasNamed = true
		}
	}
	if hasNamed {
		out = append(out, aclEntry{tag: aclMask, perm: mask, id: aclUndef})
	}
	if err := unix.Setxattr(path, aclXattr, encodeACL(out), 0); err != nil {
		return fmt.Errorf("set acl on %s: %w", path, err)
	}
	return nil
}

// grantRead lets uid read path (owner and others keep their bits).
func grantRead(path string, uid int) error {
	if uid <= 0 {
		return nil
	}
	aclMu.Lock()
	defer aclMu.Unlock()
	entries, err := readACL(path)
	if err != nil {
		return err
	}
	for i, e := range entries {
		if e.tag == aclUser && e.id == uint32(uid) {
			entries[i].perm |= aclRead
			return writeACL(path, entries)
		}
	}
	entries = append(entries, aclEntry{tag: aclUser, perm: aclRead, id: uint32(uid)})
	return writeACL(path, entries)
}

// revokeRead removes uid's entry from path's ACL.
func revokeRead(path string, uid int) error {
	if uid <= 0 {
		return nil
	}
	aclMu.Lock()
	defer aclMu.Unlock()
	entries, err := readACL(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	kept := entries[:0]
	for _, e := range entries {
		if !(e.tag == aclUser && e.id == uint32(uid)) {
			kept = append(kept, e)
		}
	}
	return writeACL(path, kept)
}

// readableBy reports whether uid can read path by mode bits or ACL.
func readableBy(path string, uid int) bool {
	st, err := os.Stat(path)
	if err != nil {
		return false
	}
	if st.Mode().Perm()&0o004 != 0 {
		return true
	}
	entries, err := readACL(path)
	if err != nil {
		return false
	}
	for _, e := range entries {
		if e.tag == aclUser && e.id == uint32(uid) && e.perm&aclRead != 0 {
			return true
		}
	}
	return false
}
