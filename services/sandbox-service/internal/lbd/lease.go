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

// LeaseInfo is one live lease: its holder and its expiry in the lease
// store's own clock (only ever compared with itself, so director and
// control-plane clocks never need to agree).
type LeaseInfo struct {
	Holder  string
	Expires int64
}

// LeaseTracker decides each group's track file (review of PR #120, second
// round). A director faults a group only when it is MASTER of it AND the
// lease holder (another live director) RENEWED the lease after this node
// became MASTER: proof that the holder is alive and also MASTER, i.e. a real
// split brain. Once raised, the fault is LATCHED for as long as that other
// director holds the lease, whatever the local VRRP state, so FAULT taking
// MASTER away can never clear it and flap the group. In steady state the
// BACKUP side is not faulted, so a failover is plain VRRP (≈ 3 s, LLD §2.14).
//
// "Renewed after" is decided without clocks across nodes: the first lease
// observation whose request was SENT after the local MASTER transition is a
// baseline; a later one with a larger expiry is a renewal after it.
type LeaseTracker struct {
	Self        string
	masterSince map[int]time.Time
	baseline    map[int]int64
	latched     map[int]bool
}

// Observe takes one lease answer (for a request sent at sentAt, with the
// keepalived states read just before) and returns the fault per group.
func (t *LeaseTracker) Observe(sentAt time.Time, groups []int, states map[int]string, leases map[string]LeaseInfo) map[int]bool {
	if t.masterSince == nil {
		t.masterSince, t.baseline, t.latched = map[int]time.Time{}, map[int]int64{}, map[int]bool{}
	}
	for g, st := range states {
		if st == "MASTER" {
			if _, ok := t.masterSince[g]; !ok {
				t.masterSince[g] = sentAt
				delete(t.baseline, g)
			}
		} else {
			delete(t.masterSince, g)
			delete(t.baseline, g)
		}
	}
	for g := range t.masterSince {
		if states[g] != "MASTER" {
			delete(t.masterSince, g)
			delete(t.baseline, g)
		}
	}
	all := map[int]bool{}
	for _, g := range groups {
		all[g] = true
	}
	for gs := range leases {
		if g, err := strconv.Atoi(gs); err == nil {
			all[g] = true
		}
	}
	out := map[int]bool{}
	for g := range all {
		l := leases[strconv.Itoa(g)]
		if l.Holder == "" || l.Holder == t.Self {
			delete(t.latched, g)
			delete(t.baseline, g)
			out[g] = false
			continue
		}
		if t.latched[g] {
			out[g] = true
			continue
		}
		since, master := t.masterSince[g]
		if !master || sentAt.Before(since) {
			out[g] = false
			continue
		}
		b, ok := t.baseline[g]
		if !ok {
			t.baseline[g] = l.Expires
			out[g] = false
			continue
		}
		if l.Expires > b {
			t.latched[g] = true
			out[g] = true
			continue
		}
		out[g] = false
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

// DefaultLeaseTTL matches fleet_directors.LEASE_SECONDS; the holder renews
// every heartbeat (0.5 s), so a dead holder's lease lapses within 2 s.
const DefaultLeaseTTL = 2 * time.Second

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
func (f FileLeases) Acquire(self string, masters []int) (map[string]LeaseInfo, error) {
	ttl := f.TTL
	if ttl <= 0 {
		ttl = DefaultLeaseTTL
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
	live := map[string]LeaseInfo{}
	for k, l := range table {
		if l.Expires >= now {
			live[k] = LeaseInfo{Holder: l.Holder, Expires: l.Expires}
		}
	}
	return live, nil
}
