// Package procscan finds running processes by their argv, so boss-hostd can
// re-adopt VMs after a crash with no local state (LLD §3.16.1, §3.21: "/proc
// and netlink scans rebuild it"). On Linux it reads /proc; elsewhere (the
// macOS process backend) it asks ps.
package procscan

import (
	"errors"
	"syscall"
)

// Proc is one running process.
type Proc struct {
	PID  int
	Args []string
}

// Flag returns the value of "--name=value" or "--name value" in args.
func (p Proc) Flag(name string) string {
	prefix := "--" + name + "="
	for i, a := range p.Args {
		if len(a) > len(prefix) && a[:len(prefix)] == prefix {
			return a[len(prefix):]
		}
		if a == "--"+name && i+1 < len(p.Args) {
			return p.Args[i+1]
		}
	}
	return ""
}

// Find returns every process for which match is true.
func Find(match func(Proc) bool) ([]Proc, error) {
	all, err := list()
	if err != nil {
		return nil, err
	}
	var out []Proc
	for _, p := range all {
		if match(p) {
			out = append(out, p)
		}
	}
	return out, nil
}

// Alive reports whether pid exists and is not a zombie.
func Alive(pid int) bool {
	if pid <= 0 {
		return false
	}
	err := syscall.Kill(pid, 0)
	if err != nil && !errors.Is(err, syscall.EPERM) {
		return false
	}
	return !zombie(pid)
}
