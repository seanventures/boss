package vmm

import (
	"errors"
	"fmt"
	"hash/fnv"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
)

// Jailer runs every Firecracker VM under Firecracker's jailer (LLD §3.18,
// S14): a chroot per VM under ChrootBase, a unique uid and gid per VM, a new
// PID namespace, a network namespace, cgroup v2 cpu.max and memory.max (VM
// plus 32 MiB), fsize and no-file limits, and Firecracker's default seccomp
// filters (never --no-seccomp, never a custom filter). API-socket mode: the
// socket lives inside the chroot, owned by the VM uid, and only boss-hostd
// (root) reaches it; snapshots need it.
type Jailer struct {
	Bin        string
	ChrootBase string // keep short: Unix socket paths inside are ≤ 107 bytes
	// UIDBase/UIDRange is the uid (and gid) pool; a VM's ids are derived from
	// its id and never collide with another running VM's.
	UIDBase, UIDRange int
	ParentCgroup      string // default boss-hostd
	FsizeBytes        int64  // default 4 GiB (must exceed the memory file and the overlay)
	NoFile            int    // default 4096
	// NoNetns skips --netns (tests on hosts without /run/netns).
	NoNetns bool

	once sync.Once
	mu   sync.Mutex
	live map[int]string // uid → fcid, for VMs this process started (parallel fills)
}

// MemOverheadMiB is the cgroup memory headroom above the VM's memory.
const MemOverheadMiB = 32

// defaults fills the zero fields once (launches run in parallel).
func (j *Jailer) defaults() { j.once.Do(j.setDefaults) }

func (j *Jailer) setDefaults() {
	if j.UIDBase <= 0 {
		j.UIDBase = 100000
	}
	if j.UIDRange <= 0 {
		j.UIDRange = 10000
	}
	if j.ParentCgroup == "" {
		j.ParentCgroup = "boss-hostd"
	}
	if j.FsizeBytes <= 0 {
		j.FsizeBytes = 4 << 30
	}
	if j.NoFile <= 0 {
		j.NoFile = 4096
	}
}

// Reasons are the named Check reasons of the jailer (heartbeat facts.reasons).
const (
	ReasonJailerMissing   = "jailer_binary_missing"
	ReasonJailerNeedsRoot = "jailer_needs_root"
	ReasonCgroupV2        = "cgroup_v2_missing"
	ReasonChrootBase      = "jailer_chroot_base_unusable"
)

// Check names the first reason the jailer cannot run.
func (j *Jailer) Check() error {
	j.defaults()
	if runtime.GOOS != "linux" {
		return fmt.Errorf("%s: the jailer needs Linux", ReasonJailerMissing)
	}
	if j.Bin == "" {
		return fmt.Errorf("%s: jailer binary not configured (BOSS_HOSTD_JAILER_BIN)", ReasonJailerMissing)
	}
	if info, err := os.Stat(j.Bin); err != nil || info.IsDir() || info.Mode()&0o111 == 0 {
		return fmt.Errorf("%s: jailer binary missing or not executable at %s", ReasonJailerMissing, j.Bin)
	}
	if os.Geteuid() != 0 {
		return fmt.Errorf("%s: the jailer needs root (CAP_SYS_ADMIN, CAP_SETUID/SETGID, CAP_SYS_CHROOT, CAP_MKNOD); boss-hostd runs as uid %d", ReasonJailerNeedsRoot, os.Geteuid())
	}
	raw, err := os.ReadFile("/sys/fs/cgroup/cgroup.controllers")
	if err != nil {
		return fmt.Errorf("%s: /sys/fs/cgroup is not a cgroup v2 hierarchy", ReasonCgroupV2)
	}
	ctl := " " + strings.TrimSpace(string(raw)) + " "
	if !strings.Contains(ctl, " cpu ") || !strings.Contains(ctl, " memory ") {
		return fmt.Errorf("%s: cgroup v2 lacks the cpu or memory controller (%s)", ReasonCgroupV2, strings.TrimSpace(string(raw)))
	}
	if j.ChrootBase == "" {
		return fmt.Errorf("%s: chroot base not configured (BOSS_HOSTD_JAILER_CHROOT)", ReasonChrootBase)
	}
	if err := os.MkdirAll(j.ChrootBase, 0o755); err != nil {
		return fmt.Errorf("%s: %v", ReasonChrootBase, err)
	}
	if !j.NoNetns {
		if err := os.MkdirAll("/run/netns", 0o755); err != nil {
			return fmt.Errorf("%s: /run/netns: %v", ReasonChrootBase, err)
		}
	}
	return nil
}

// chroot is the VM's root inside the jail: <base>/firecracker/<fcid>/root.
// The jailer derives the middle component from the exec file's name, so the
// exec file is always installed as "firecracker".
func (j *Jailer) chroot(fcid string) string {
	return filepath.Join(j.ChrootBase, "firecracker", fcid, "root")
}

func (j *Jailer) jailDir(fcid string) string { return filepath.Join(j.ChrootBase, "firecracker", fcid) }

// uidFor derives a stable uid from the VM id and moves past any uid another
// jail directory already owns or another in-flight launch of this process
// has reserved. The reservation is released at Stop.
func (j *Jailer) uidFor(fcid string) int {
	j.defaults()
	j.mu.Lock()
	defer j.mu.Unlock()
	if j.live == nil {
		j.live = map[int]string{}
	}
	h := fnv.New32a()
	_, _ = h.Write([]byte(fcid))
	start := int(h.Sum32() % uint32(j.UIDRange))
	used := map[int]bool{}
	for uid, owner := range j.live {
		if owner != fcid {
			used[uid] = true
		}
	}
	if ents, err := os.ReadDir(filepath.Join(j.ChrootBase, "firecracker")); err == nil {
		for _, e := range ents {
			if e.Name() == fcid {
				continue
			}
			if st, err := os.Stat(filepath.Join(j.ChrootBase, "firecracker", e.Name(), "root")); err == nil {
				if sys, ok := st.Sys().(*syscall.Stat_t); ok {
					used[int(sys.Uid)] = true
				}
			}
		}
	}
	for i := 0; i < j.UIDRange; i++ {
		uid := j.UIDBase + (start+i)%j.UIDRange
		if !used[uid] {
			j.live[uid] = fcid
			return uid
		}
	}
	return j.UIDBase + start
}

// release frees a uid reservation (Stop, or a failed launch).
func (j *Jailer) release(uid int) {
	if j == nil || uid <= 0 {
		return
	}
	j.mu.Lock()
	delete(j.live, uid)
	j.mu.Unlock()
}

// argv builds the jailer command line. The jailer itself adds --id and
// --api-sock (/run/firecracker.socket inside the chroot) to Firecracker's
// arguments; fcArgs are appended after "--".
func (j *Jailer) argv(fcid, fcBin string, uid int, vcpu, memMiB int, netns string, fcArgs ...string) []string {
	j.defaults()
	cpuMax := fmt.Sprintf("%d 100000", vcpu*100000)
	memMax := strconv.FormatInt(int64(memMiB+MemOverheadMiB)<<20, 10)
	args := []string{j.Bin, "--id", fcid, "--exec-file", fcBin, "--uid", strconv.Itoa(uid), "--gid", strconv.Itoa(uid),
		"--chroot-base-dir", j.ChrootBase, "--cgroup-version", "2", "--parent-cgroup", j.ParentCgroup,
		"--cgroup", "cpu.max=" + cpuMax, "--cgroup", "memory.max=" + memMax,
		"--resource-limit", "fsize=" + strconv.FormatInt(j.FsizeBytes, 10),
		"--resource-limit", "no-file=" + strconv.Itoa(j.NoFile),
		"--new-pid-ns"}
	if netns != "" {
		args = append(args, "--netns", netns)
	}
	args = append(args, "--")
	return append(args, fcArgs...)
}

// cgroupDir is the VM's cgroup the jailer created.
func (j *Jailer) cgroupDir(fcid string) string {
	return filepath.Join("/sys/fs/cgroup", j.ParentCgroup, fcid)
}

// pidFile is where the jailer (with --new-pid-ns) writes Firecracker's pid.
func (j *Jailer) pidFile(fcid string) string { return filepath.Join(j.chroot(fcid), "firecracker.pid") }

// errNotJailed marks a VM that runs outside the jailer.
var errNotJailed = errors.New("vm is not jailed")
