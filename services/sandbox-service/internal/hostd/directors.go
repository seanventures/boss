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
func (a *Agent) pushMember(ctx context.Context, dv hostapi.DesiredVM, weight int, reason string) {
	t, ok := pushTargetOf(dv)
	if !ok {
		return
	}
	method := http.MethodPut
	if weight < 0 {
		method = http.MethodDelete
		weight = 0
	}
	m := hostapi.VIPMember{VMIP: t.vmIP, Weight: weight, Ports: t.ports, HostID: a.HostID(), Reason: reason}
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

// withdrawVM is the watchdog's step 1 (+2): weight 0 at once; on confirmed
// death also delete. Runs in its own goroutine so the state machine never
// waits on the network.
func (a *Agent) withdrawVM(dv hostapi.DesiredVM, reason string, dead bool) {
	if _, ok := pushTargetOf(dv); !ok {
		return
	}
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		a.pushMember(ctx, dv, 0, reason)
		if dead {
			a.pushMember(ctx, dv, -1, reason)
		}
	}()
}

// registerVM pushes the replica's weight when it turns serving.
func (a *Agent) registerVM(dv hostapi.DesiredVM) {
	if _, ok := pushTargetOf(dv); !ok {
		return
	}
	w := dv.Weight
	if w <= 0 {
		w = 1
	}
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		a.pushMember(ctx, dv, w, "serving")
	}()
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
