package hostd

import (
	"context"
	"os"
	"runtime"
	"strconv"
	"strings"
	"syscall"

	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanheiney/boss/services/sandbox-service/internal/vmm"
)

// facts measures the hardening facts. Each false fact carries its reason.
func (a *Agent) facts(ctx context.Context) hostapi.Attestation {
	f := hostapi.Attestation{Reasons: map[string]string{}}
	if fd, err := os.OpenFile("/dev/kvm", os.O_RDWR, 0); err == nil {
		fd.Close()
		f.KVM = true
	} else {
		f.Reasons["kvm_live"] = "/dev/kvm not usable"
	}
	fc := a.cfg.Manager.Name() == "firecracker"
	att, measured := a.cfg.Manager.(vmm.Attestor)
	if fc {
		if err := a.cfg.Manager.Check(); err == nil {
			f.FirecrackerLive = true
			// Measured, not configured: every running VM must show
			// "Seccomp: 2" (boss-hostd never passes --no-seccomp or a filter).
			if !measured {
				f.Reasons["seccomp_default"] = "backend cannot measure seccomp"
			} else if on, why := att.SeccompOn(); on {
				f.Seccomp = true
			} else {
				f.Reasons["seccomp_default"] = why
			}
		} else {
			f.Reasons["firecracker_live"] = err.Error()
			f.Reasons["seccomp_default"] = "firecracker not live"
		}
	} else {
		f.Reasons["firecracker_live"] = "backend is " + a.cfg.Manager.Name()
		f.Reasons["seccomp_default"] = "backend is " + a.cfg.Manager.Name()
	}
	if measured && fc {
		if on, why := att.JailerOn(); on {
			f.Jailer = true
		} else {
			f.Reasons["jailer_on"] = why
		}
	} else {
		f.Reasons["jailer_on"] = "backend is " + a.cfg.Manager.Name()
	}
	if v, ok := readTrim("/sys/devices/system/cpu/smt/active"); ok {
		f.SMTOff = v == "0"
		if !f.SMTOff {
			f.Reasons["smt_off"] = "SMT is active"
		}
	} else {
		f.Reasons["smt_off"] = "unknown (no /sys smt control)"
	}
	if v, ok := readTrim("/sys/kernel/mm/ksm/run"); ok {
		f.KSMOff = v == "0"
		if !f.KSMOff {
			f.Reasons["ksm_off"] = "KSM is running"
		}
	} else if runtime.GOOS == "linux" {
		f.KSMOff = true // kernel built without KSM
	} else {
		f.Reasons["ksm_off"] = "unknown on " + runtime.GOOS
	}
	if raw, err := os.ReadFile("/proc/swaps"); err == nil {
		lines := strings.Split(strings.TrimSpace(string(raw)), "\n")
		f.SwapOff = len(lines) <= 1
		if !f.SwapOff {
			f.Reasons["swap_off"] = "swap is enabled"
		}
	} else {
		f.Reasons["swap_off"] = "unknown on " + runtime.GOOS
	}
	f.StrictSecrets = a.cfg.StrictSecrets
	if !f.StrictSecrets {
		f.Reasons["strict_secrets"] = "strict-secrets mode is off"
	}
	if ok, reason := a.cfg.Net.PolicyOK(ctx); ok && fc {
		f.NetPolicyOK = true
	} else if reason != "" {
		f.Reasons["net_policy_ok"] = reason
	} else {
		f.Reasons["net_policy_ok"] = "backend is " + a.cfg.Manager.Name()
	}
	if f.NetPolicyOK {
		delete(f.Reasons, "net_policy_ok")
	}
	return f
}

func readTrim(path string) (string, bool) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", false
	}
	return strings.TrimSpace(string(raw)), true
}

func (a *Agent) capacity() hostapi.Capacity {
	return hostapi.Capacity{VCPU: runtime.NumCPU(), MemMiB: memTotalMiB(), MaxVMs: a.cfg.MaxVMs}
}

func meminfo() map[string]int64 {
	out := map[string]int64{}
	raw, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return out
	}
	for _, line := range strings.Split(string(raw), "\n") {
		f := strings.Fields(line)
		if len(f) >= 2 {
			n, _ := strconv.ParseInt(f[1], 10, 64)
			out[strings.TrimSuffix(f[0], ":")] = n
		}
	}
	return out
}

func memTotalMiB() int { return int(meminfo()["MemTotal"] / 1024) }

func pressure(dir string) hostapi.Pressure {
	var p hostapi.Pressure
	m := meminfo()
	if total := m["MemTotal"]; total > 0 {
		p.MemUsedPct = 100 * float64(total-m["MemAvailable"]) / float64(total)
	}
	var st syscall.Statfs_t
	if dir != "" && syscall.Statfs(dir, &st) == nil && st.Blocks > 0 {
		p.DiskUsedPct = 100 * float64(st.Blocks-st.Bavail) / float64(st.Blocks)
	}
	if v, ok := readTrim("/proc/loadavg"); ok {
		if f := strings.Fields(v); len(f) > 0 {
			p.Load1, _ = strconv.ParseFloat(f[0], 64)
		}
	}
	return p
}
