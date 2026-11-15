//go:build !linux

package vmm

import "errors"

var errNoACL = errors.New("posix acls are linux-only")

func grantRead(string, int) error  { return errNoACL }
func revokeRead(string, int) error { return nil }
func readableBy(string, int) bool  { return false }
