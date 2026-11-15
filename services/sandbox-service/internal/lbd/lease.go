package lbd

import (
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
)

type (
	hostapiGroup = hostapi.VRRPGroup
	hostapiHost  = hostapi.DirectorHost
)

// State files under the state dir (default /run/boss-lbd):
//
//	state/<group>  MASTER | BACKUP | FAULT | STOP   written by `boss-lbd notify` (keepalived)
//	track/<group>  ok | fault                        written by the heartbeat loop from the lease
//
// `boss-lbd track --group N` (keepalived's vrrp_script, every second) exits
// non-zero only when track/<group> says fault: this director is MASTER of
// the group but another director holds the group's lease in Postgres. The
// instance then goes FAULT and drops the VIPs, which ends a split brain
// (LLD §3.14.4). A missing file, an unreachable control plane or a lease
// nobody holds never fault: the lease exists to break a tie, not to take
// every VIP down with the control plane (decision 30 in HOST_API.md).
const (
	dirState = "state"
	dirTrack = "track"
)

// WriteNotify records keepalived's state for one group (the notify script).
func WriteNotify(stateDir string, group int, state string) error {
	dir := filepath.Join(stateDir, dirState)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, strconv.Itoa(group)), []byte(strings.ToUpper(strings.TrimSpace(state))), 0o644)
}

// ReadStates lists group → keepalived state.
func ReadStates(stateDir string) map[int]string {
	out := map[int]string{}
	entries, err := os.ReadDir(filepath.Join(stateDir, dirState))
	if err != nil {
		return out
	}
	for _, e := range entries {
		g, err := strconv.Atoi(e.Name())
		if err != nil {
			continue
		}
		raw, err := os.ReadFile(filepath.Join(stateDir, dirState, e.Name()))
		if err == nil {
			out[g] = strings.TrimSpace(string(raw))
		}
	}
	return out
}

// Masters are the groups keepalived reports MASTER for, sorted.
func Masters(states map[int]string) []int {
	var out []int
	for g, s := range states {
		if s == "MASTER" {
			out = append(out, g)
		}
	}
	sort.Ints(out)
	return out
}

// TrackDecision is the rule (review of PR #120): a group is FAULT here for
// as long as ANOTHER live director holds its lease, whatever keepalived's
// local state. Tracking only while MASTER flapped under a real split brain:
// FAULT → not MASTER → fault cleared → MASTER again (with GARPs) every few
// seconds. `groups` are every group the desired state knows; a group with
// no lease, or our own, is ok. Leases arrive already filtered to live ones
// (unexpired), so a dead holder's lease lapses within its TTL and the fault
// clears by itself.
func TrackDecision(selfID string, groups []int, leases map[string]string) map[int]bool {
	out := map[int]bool{}
	for _, g := range groups {
		out[g] = false
	}
	for gs, holder := range leases {
		g, err := strconv.Atoi(gs)
		if err != nil {
			continue
		}
		out[g] = holder != "" && holder != selfID
	}
	return out
}

// WriteTrack writes track/<group> for every decided group.
func WriteTrack(stateDir string, faults map[int]bool) error {
	dir := filepath.Join(stateDir, dirTrack)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	for g, fault := range faults {
		val := "ok"
		if fault {
			val = "fault"
		}
		if err := os.WriteFile(filepath.Join(dir, strconv.Itoa(g)), []byte(val), 0o644); err != nil {
			return err
		}
	}
	return nil
}

// Track is the vrrp_script: 0 unless the group is marked fault.
func Track(stateDir string, group int) int {
	raw, err := os.ReadFile(filepath.Join(stateDir, dirTrack, strconv.Itoa(group)))
	if err != nil {
		return 0
	}
	if strings.TrimSpace(string(raw)) == "fault" {
		return 1
	}
	return 0
}

// FileLeases is the lab's lease store (no control plane): the same rules as
// fleet_directors.heartbeat — take a group's lease when it is free, expired
// or ours; release our lease of a group we are no longer MASTER of — kept
// in one JSON file under flock, shared by both directors' namespaces.
type FileLeases struct {
	Path string
	TTL  time.Duration
}

type fileLease struct {
	Holder  string `json:"holder"`
	Expires int64  `json:"expires_unix_nano"`
}

// Acquire renews/takes the leases of masters and answers every live holder.
func (f FileLeases) Acquire(self string, masters []int) (map[string]string, error) {
	ttl := f.TTL
	if ttl <= 0 {
		ttl = 5 * time.Second
	}
	fd, err := os.OpenFile(f.Path, os.O_RDWR|os.O_CREATE, 0o644)
	if err != nil {
		return nil, err
	}
	defer fd.Close()
	if err := syscall.Flock(int(fd.Fd()), syscall.LOCK_EX); err != nil {
		return nil, err
	}
	defer syscall.Flock(int(fd.Fd()), syscall.LOCK_UN) //nolint:errcheck
	raw, _ := io.ReadAll(fd)
	table := map[string]fileLease{}
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &table)
	}
	now := time.Now().UnixNano()
	mine := map[string]bool{}
	for _, g := range masters {
		k := strconv.Itoa(g)
		mine[k] = true
		cur, ok := table[k]
		if !ok || cur.Holder == self || cur.Expires < now {
			table[k] = fileLease{Holder: self, Expires: now + int64(ttl)}
		}
	}
	for k, l := range table {
		if l.Holder == self && !mine[k] {
			delete(table, k) // no longer MASTER of it: let the peer take it now
		}
	}
	out, _ := json.Marshal(table)
	if _, err := fd.Seek(0, 0); err != nil {
		return nil, err
	}
	if err := fd.Truncate(0); err != nil {
		return nil, err
	}
	if _, err := fd.Write(out); err != nil {
		return nil, err
	}
	live := map[string]string{}
	for k, l := range table {
		if l.Expires >= now {
			live[k] = l.Holder
		}
	}
	return live, nil
}
