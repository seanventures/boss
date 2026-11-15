package lbd

import (
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

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

// TrackDecision is the rule: fault iff MASTER and the lease is another's.
func TrackDecision(selfID string, masters []int, leases map[string]string) map[int]bool {
	isMaster := map[int]bool{}
	for _, g := range masters {
		isMaster[g] = true
	}
	out := map[int]bool{}
	for gs, holder := range leases {
		g, err := strconv.Atoi(gs)
		if err != nil {
			continue
		}
		out[g] = isMaster[g] && holder != "" && holder != selfID
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
