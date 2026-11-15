//go:build !linux

package guest

import "errors"

// VsockTransport is Linux-only; the process backend uses UnixTransport.
type VsockTransport struct{ UnixTransport }

func InitSystem() error                  { return errors.New("init mode is Linux-only") }
func ReplicaNetSetup(b *Bootstrap) error { return errors.New("replica NIC setup is Linux-only") }
