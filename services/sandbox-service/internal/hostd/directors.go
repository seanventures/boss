package hostd

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanheiney/boss/services/sandbox-service/internal/lbd"
)

// Director pushes (P8b-10, LLD §3.14.2 "withdraw in under a second"). The
// watchdog sees a VM die at once (process exit) or within 1.5 s (3 missed
// pings) and then:
//
//  1. pushes weight 0 to every director in desired_state.director_push
//     (mTLS with the host certificate; ≈10 ms), so no new connection lands;
//  2. on confirmed death deletes the real server there (expire_nodest_conn=1
//     on the director resets that VM's connections, clients reconnect);
//  3. records the same at the control plane (PUT/DELETE /v1/vips/…), the
//     durable record, best effort: /observed carries the state anyway.
//
// A replica that turns serving is registered the same way (its weight), so
// it takes traffic before the next desired-state feed reaches the directors.
type pushTarget struct {
	vip     string
	ports   []int
	vmIP    string
	replica string
}

func pushTargetOf(dv hostapi.DesiredVM) (pushTarget, bool) {
	if dv.Mode != hostapi.ModeReplica || dv.VIP == "" || dv.VMIP == "" || dv.ReplicaID == "" {
		return pushTarget{}, false
	}
	return pushTarget{vip: dv.VIP, ports: dv.VIPPorts, vmIP: dv.VMIP, replica: dv.ReplicaID}, true
}

// directors is the current push list (from desired state).
func (a *Agent) directors() []hostapi.DirectorEndpoint {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.desired == nil {
		return nil
	}
	return a.desired.DirectorPush
}

func (a *Agent) pusher() *lbd.Pusher {
	a.pushMu.Lock()
	defer a.pushMu.Unlock()
	c := a.creds.Load()
	if c == nil {
		return nil
	}
	if a.push == nil || a.pushCA != c.FleetCAPEM {
		pool := x509.NewCertPool()
		pool.AppendCertsFromPEM([]byte(c.FleetCAPEM))
		a.push = &lbd.Pusher{FleetCA: pool, Timeout: 500 * time.Millisecond, Cert: func() *tls.Certificate {
			if c := a.creds.Load(); c != nil {
				cert := c.TLSCertificate()
				return &cert
			}
			return nil
		}}
		a.pushCA = c.FleetCAPEM
	}
	return a.push
}

// pushMember sends one member call to every director and the control plane.
// weight < 0 means DELETE. It returns once every director answered or timed
// out (≤ 500 ms), so the caller's state machine is never blocked for long.
func (a *Agent) pushMember(ctx context.Context, dv hostapi.DesiredVM, weight int, reason string, seq int64) {
	t, ok := pushTargetOf(dv)
	if !ok {
		return
	}
	method := http.MethodPut
	if weight < 0 {
		method = http.MethodDelete
		weight = 0
	}
	m := hostapi.VIPMember{VMIP: t.vmIP, Weight: weight, Ports: t.ports, HostID: a.HostID(), Reason: reason, Seq: seq}
	start := time.Now()
	eps := a.directors()
	if p := a.pusher(); p != nil && len(eps) > 0 {
		res := p.Push(ctx, eps, method, t.vip, url.PathEscape(t.replica), m)
		for _, r := range res {
			if r.Err != nil {
				a.logf("director %s %s vip %s rs %s: %v", r.Director, method, t.vip, t.vmIP, r.Err)
				continue
			}
			a.logf("director %s %s vip %s rs %s weight %d (%s) in %s", r.Director, method, t.vip, t.vmIP, weight,
				reason, r.Latency.Round(time.Microsecond))
		}
		a.event(dv.VMID, "director_push", "", "", map[string]string{"method": method, "vip": t.vip, "weight": itoa(weight),
			"reason": reason, "directors": itoa(len(eps)), "ms": itoa(int(time.Since(start).Milliseconds()))})
	}
	if a.client != nil {
		cctx, cancel := context.WithTimeout(ctx, 2*time.Second)
		defer cancel()
		if err := a.client.VIPMember(cctx, method, t.vip, t.replica, m); err != nil {
			a.logf("control plane %s vip %s member %s: %v", method, t.vip, t.replica, err)
		}
	}
}

// Member pushes are serialized (review of PR #120): each decision takes a
// sequence number at once (on the watchdog's path, so the order is the
// order of decisions), then runs on that member's own lane; a lane skips a
// decision a newer one has superseded, and the directors and the control
// plane refuse an older sequence than one they already applied, so the
// latest decision wins even if two pushes cross on the network.
func (a *Agent) nextSeq() int64 {
	for {
		last := a.pushSeq.Load()
		next := time.Now().UnixNano()
		if next <= last {
			next = last + 1
		}
		if a.pushSeq.CompareAndSwap(last, next) {
			return next
		}
	}
}

type memberStep struct {
	weight int // < 0 = DELETE
	reason string
}

// enqueue runs steps for one member in order on its lane, unless a newer
// decision for the member arrives first.
func (a *Agent) enqueue(dv hostapi.DesiredVM, steps ...memberStep) {
	t, ok := pushTargetOf(dv)
	if !ok {
		return
	}
	key := t.vip + "|" + t.vmIP
	seqs := make([]int64, len(steps))
	for i := range steps {
		seqs[i] = a.nextSeq()
	}
	a.pushMu.Lock()
	if a.lanes == nil {
		a.lanes = map[string]*memberLane{}
	}
	lane := a.lanes[key]
	if lane == nil {
		lane = &memberLane{}
		a.lanes[key] = lane
	}
	lane.latest = seqs[len(seqs)-1]
	a.pushMu.Unlock()
	go func() {
		lane.mu.Lock()
		defer lane.mu.Unlock()
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		for i, st := range steps {
			a.pushMu.Lock()
			superseded := lane.latest > seqs[len(seqs)-1]
			a.pushMu.Unlock()
			if superseded {
				return // a newer decision for this member is queued
			}
			a.pushMember(ctx, dv, st.weight, st.reason, seqs[i])
		}
	}()
}

type memberLane struct {
	mu     sync.Mutex
	latest int64 // guarded by Agent.pushMu
}

// withdrawVM is the watchdog's step 1 (+2): weight 0 at once; on confirmed
// death also delete. Off the state machine's path, serialized per member.
func (a *Agent) withdrawVM(dv hostapi.DesiredVM, reason string, dead bool) {
	if dead {
		a.enqueue(dv, memberStep{0, reason}, memberStep{-1, reason})
		return
	}
	a.enqueue(dv, memberStep{0, reason})
}

// registerVM pushes the replica's weight when it turns serving.
func (a *Agent) registerVM(dv hostapi.DesiredVM) {
	w := dv.Weight
	if w <= 0 {
		w = 1
	}
	a.enqueue(dv, memberStep{w, "serving"})
}

// confirmedDeath: the process is gone or the control channel is silent.
func confirmedDeath(reason string) bool {
	return reason == "process_exit" || strings.HasPrefix(reason, "missed_pings")
}

func itoa(n int) string { return strconv.Itoa(n) }

// detectAddress is the host's own address toward the control plane: the
// source the kernel picks for a UDP "connection" to it. BOSS_HOSTD_ADDRESS
// overrides it (a NAT, or a dedicated fleet interface).
func detectAddress(cpURL string) string {
	u, err := url.Parse(cpURL)
	if err != nil || u.Host == "" {
		return ""
	}
	host := u.Hostname()
	port := u.Port()
	if port == "" {
		port = "443"
	}
	c, err := net.DialTimeout("udp", net.JoinHostPort(host, port), time.Second)
	if err != nil {
		return ""
	}
	defer c.Close()
	if la, ok := c.LocalAddr().(*net.UDPAddr); ok && la.IP.To4() != nil && !la.IP.IsLoopback() {
		return la.IP.String()
	}
	return ""
}
