//go:build linux

package vmm

import (
	"errors"
	"io"
	"os"

	"golang.org/x/sys/unix"
)

// copySparseFast copies only the data extents of src (SEEK_DATA/SEEK_HOLE),
// so a mostly-empty 1 GiB overlay costs a few megabytes of I/O. Falls back
// to the portable zero-scanning copy when the filesystem has no hole support.
func copySparseFast(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	st, err := in.Stat()
	if err != nil {
		return err
	}
	size := st.Size()
	if _, err := unix.Seek(int(in.Fd()), 0, unix.SEEK_HOLE); err != nil {
		if errors.Is(err, unix.ENXIO) || errors.Is(err, unix.EINVAL) {
			return copySparse(src, dst)
		}
		return err
	}
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	defer out.Close()
	buf := make([]byte, 1<<20)
	var off int64
	for off < size {
		data, err := unix.Seek(int(in.Fd()), off, unix.SEEK_DATA)
		if err != nil {
			if errors.Is(err, unix.ENXIO) {
				break // only a hole remains
			}
			return err
		}
		hole, err := unix.Seek(int(in.Fd()), data, unix.SEEK_HOLE)
		if err != nil {
			return err
		}
		for pos := data; pos < hole; {
			n := int64(len(buf))
			if hole-pos < n {
				n = hole - pos
			}
			got, err := in.ReadAt(buf[:n], pos)
			if err != nil && err != io.EOF {
				return err
			}
			if got == 0 {
				break
			}
			if !allZero(buf[:got]) {
				if _, err := out.WriteAt(buf[:got], pos); err != nil {
					return err
				}
			}
			pos += int64(got)
		}
		off = hole
	}
	return out.Truncate(size)
}
