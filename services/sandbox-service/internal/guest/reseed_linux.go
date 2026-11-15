//go:build linux

package guest

import (
	"encoding/binary"
	"fmt"
	"os"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

// reseed mixes the host's seed into the kernel input pool with entropy
// credit (RNDADDENTROPY) and re-keys the CRNG right away (RNDRESEEDCRNG), so
// a restored clone's /dev/urandom and getrandom() diverge from its siblings
// before anything reads them. VMGenID (a 5.18+ guest driver) does the same
// on resume where present; the seed does not depend on it. Needs root, which
// PID 1 has; otherwise the seed is written uncredited (best effort).
func reseed(seed []byte) (bool, error) {
	if len(seed) == 0 {
		return false, nil
	}
	f, err := os.OpenFile("/dev/urandom", os.O_RDWR, 0)
	if err != nil {
		return false, err
	}
	defer f.Close()
	// struct rand_pool_info { int entropy_count; int buf_size; __u32 buf[]; }
	buf := make([]byte, 8+len(seed))
	binary.LittleEndian.PutUint32(buf, uint32(len(seed)*8))
	binary.LittleEndian.PutUint32(buf[4:], uint32(len(seed)))
	copy(buf[8:], seed)
	if _, _, errno := unix.Syscall(unix.SYS_IOCTL, f.Fd(), uintptr(unix.RNDADDENTROPY), uintptr(unsafe.Pointer(&buf[0]))); errno != 0 {
		// Not root: the write still mixes, uncredited.
		if _, werr := f.Write(seed); werr != nil {
			return false, fmt.Errorf("RNDADDENTROPY: %v; write: %w", errno, werr)
		}
		return false, nil
	}
	if _, _, errno := unix.Syscall(unix.SYS_IOCTL, f.Fd(), uintptr(unix.RNDRESEEDCRNG), 0); errno != 0 {
		return false, fmt.Errorf("RNDRESEEDCRNG: %v", errno)
	}
	return true, nil
}

// setClock moves the wall clock to the host's time when it is off by more
// than a second (a restored guest wakes up at its snapshot's time).
func setClock(hostUnixNano int64) error {
	if hostUnixNano == 0 {
		return nil
	}
	host := time.Unix(0, hostUnixNano)
	if d := host.Sub(time.Now()); d > -time.Second && d < time.Second {
		return nil
	}
	tv := unix.NsecToTimeval(hostUnixNano)
	return unix.Settimeofday(&tv)
}
