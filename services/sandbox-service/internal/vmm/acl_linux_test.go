//go:build linux

package vmm

import (
	"os"
	"path/filepath"
	"testing"
)

func TestACLEncodeRoundTrip(t *testing.T) {
	entries := []aclEntry{{tag: aclOther, perm: 0, id: aclUndef}, {tag: aclUser, perm: aclRead, id: 100042},
		{tag: aclUserObj, perm: 6, id: aclUndef}, {tag: aclGroupObj, perm: 0, id: aclUndef}}
	raw := encodeACL(entries)
	if len(raw) != 4+8*len(entries) {
		t.Fatalf("encoded size %d", len(raw))
	}
	back, err := decodeACL(raw)
	if err != nil {
		t.Fatal(err)
	}
	if back[0].tag != aclUserObj || back[1].tag != aclUser || back[1].id != 100042 || back[len(back)-1].tag != aclOther {
		t.Fatalf("entries not sorted by tag: %+v", back)
	}
	if _, err := decodeACL(raw[:7]); err == nil {
		t.Fatal("malformed acl accepted")
	}
}

// On a filesystem with ACLs, a 0600 file becomes readable by exactly the
// granted uid and nobody else; revoke removes the grant.
func TestGrantAndRevokeRead(t *testing.T) {
	path := filepath.Join(t.TempDir(), "mem")
	if err := os.WriteFile(path, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := grantRead(path, 100042); err != nil {
		t.Skipf("no ACL support here: %v", err)
	}
	if !readableBy(path, 100042) || readableBy(path, 100043) {
		t.Fatal("grant must apply to exactly the given uid")
	}
	if err := revokeRead(path, 100042); err != nil {
		t.Fatal(err)
	}
	if readableBy(path, 100042) {
		t.Fatal("revoked uid still reads")
	}
	st, _ := os.Stat(path)
	if st.Mode().Perm()&0o007 != 0 {
		t.Fatalf("others must have no access: %o", st.Mode().Perm())
	}
}
