//go:build !linux

package guest

import (
	"errors"
	"os"
)

// VsockTransport is Linux-only; the process backend uses UnixTransport.
type VsockTransport struct{ UnixTransport }

func InitSystem() error                  { return errors.New("init mode is Linux-only") }
func ReplicaNetSetup(b *Bootstrap) error { return errors.New("replica NIC setup is Linux-only") }

// reseed is best effort off Linux: write the seed to the system RNG device.
func reseed(seed []byte) (bool, error) {
	if len(seed) == 0 {
		return false, nil
	}
	f, err := os.OpenFile("/dev/urandom", os.O_WRONLY, 0)
	if err != nil {
		return false, err
	}
	defer f.Close()
	_, err = f.Write(seed)
	return false, err
}

func setClock(int64) error { return nil }
