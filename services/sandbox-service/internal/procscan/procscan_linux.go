//go:build linux

package procscan

import (
	"bytes"
	"os"
	"strconv"
	"strings"
)

func list() ([]Proc, error) {
	ents, err := os.ReadDir("/proc")
	if err != nil {
		return nil, err
	}
	var out []Proc
	for _, e := range ents {
		pid, err := strconv.Atoi(e.Name())
		if err != nil {
			continue
		}
		raw, err := os.ReadFile("/proc/" + e.Name() + "/cmdline")
		if err != nil || len(raw) == 0 {
			continue
		}
		raw = bytes.TrimRight(raw, "\x00")
		out = append(out, Proc{PID: pid, Args: strings.Split(string(raw), "\x00")})
	}
	return out, nil
}

func zombie(pid int) bool {
	raw, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		return true
	}
	// pid (comm) S ... — the state follows the last ')'.
	i := bytes.LastIndexByte(raw, ')')
	if i < 0 || i+2 >= len(raw) {
		return false
	}
	return raw[i+2] == 'Z' || raw[i+2] == 'X'
}

// Root is the process's root directory ("/" unless chrooted; "" if unknown).
func Root(pid int) string {
	link, err := os.Readlink("/proc/" + strconv.Itoa(pid) + "/root")
	if err != nil {
		return ""
	}
	return link
}

// UID is the process's real uid (-1 if unknown).
func UID(pid int) int {
	raw, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/status")
	if err != nil {
		return -1
	}
	for _, line := range strings.Split(string(raw), "\n") {
		if strings.HasPrefix(line, "Uid:") {
			f := strings.Fields(line[4:])
			if len(f) > 0 {
				if n, err := strconv.Atoi(f[0]); err == nil {
					return n
				}
			}
		}
	}
	return -1
}
