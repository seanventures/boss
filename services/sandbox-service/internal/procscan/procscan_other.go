//go:build !linux

package procscan

import (
	"os/exec"
	"strconv"
	"strings"
)

func list() ([]Proc, error) {
	out, err := exec.Command("ps", "-axww", "-o", "pid=,stat=,args=").Output()
	if err != nil {
		return nil, err
	}
	var procs []Proc
	for _, line := range strings.Split(string(out), "\n") {
		f := strings.Fields(line)
		if len(f) < 3 {
			continue
		}
		pid, err := strconv.Atoi(f[0])
		if err != nil || strings.HasPrefix(f[1], "Z") {
			continue
		}
		procs = append(procs, Proc{PID: pid, Args: f[2:]})
	}
	return procs, nil
}

func zombie(pid int) bool {
	out, err := exec.Command("ps", "-o", "stat=", "-p", strconv.Itoa(pid)).Output()
	if err != nil {
		return true
	}
	return strings.HasPrefix(strings.TrimSpace(string(out)), "Z")
}

// Root is unknown off Linux.
func Root(int) string { return "" }

// UID is unknown off Linux.
func UID(int) int { return -1 }
