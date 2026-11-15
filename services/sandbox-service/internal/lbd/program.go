// Package lbd is boss-lbd, the IPVS director (LLD §3.14.2, P8b-10,
// BOSSAPP-1077). It programs the kernel's IPVS from BOSS desired state: one
// virtual service per (VIP, port), rr (wlc when slots differ), persistence
// off, GRE tunnel-mode real servers = serving replicas' vm_ip with their
// rs_weight. keepalived does VRRP only (lbd renders its config); the
// connection sync daemons run between A and B; a director lease in Postgres
// (through the control plane) ends a split brain; health checks and direct
// withdraw pushes from hosts take a dead VM out in well under a second.
package lbd

import (
	"fmt"
	"net"
	"sort"
	"sync"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanheiney/boss/services/sandbox-service/internal/lbd/ipvs"
)

// OverrideTTL bounds a pushed withdraw's precedence over desired state. The
// control plane learns of the death through /observed within ~100 ms and
// drops the member from the feed; this is the safety net if it never does.
const OverrideTTL = 30 * time.Second

// override is a host's direct push about one member, applied before the
// desired feed reflects it.
type override struct {
	weight  int
	deleted bool
	until   time.Time
}

// Programmer reconciles the IPVS table toward desired state and applies
// withdraw pushes at once.
type Programmer struct {
	be   ipvs.Backend
	logf func(string, ...any)

	mu        sync.Mutex
	desired   *hostapi.DirectorDesired
	overrides map[string]override // "<vip>|<vm_ip>" → override
	owned     map[string]bool     // service keys this director created or adopted
	pools     []*net.IPNet
}

// NewProgrammer wraps a backend.
func NewProgrammer(be ipvs.Backend, logf func(string, ...any)) *Programmer {
	if logf == nil {
		logf = func(string, ...any) {}
	}
	return &Programmer{be: be, logf: logf, overrides: map[string]override{}, owned: map[string]bool{}}
}

// Changes summarises one Apply.
type Changes struct {
	ServicesAdded, ServicesUpdated, ServicesRemoved int
	DestsAdded, DestsUpdated, DestsRemoved          int
	Errors                                          int
}

func (c Changes) String() string {
	return fmt.Sprintf("svc +%d ~%d -%d, rs +%d ~%d -%d, errors %d", c.ServicesAdded, c.ServicesUpdated,
		c.ServicesRemoved, c.DestsAdded, c.DestsUpdated, c.DestsRemoved, c.Errors)
}

func memberKey(vip, vmIP string) string { return vip + "|" + vmIP }

func service(vip string, port int, sched string) ipvs.Service {
	return ipvs.Service{Address: net.ParseIP(vip).To4(), Port: uint16(port), Protocol: 6, Scheduler: sched,
		Netmask: 0xffffffff}
}

func dest(vmIP string, port, weight int) ipvs.Destination {
	return ipvs.Destination{Address: net.ParseIP(vmIP).To4(), Port: uint16(port), Weight: weight,
		Forward: ipvs.FwdTunnel, TunType: ipvs.TunGRE}
}

// SetDesired stores the latest desired state; Apply reconciles toward it.
func (p *Programmer) SetDesired(d *hostapi.DirectorDesired) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.desired = d
	p.pools = p.pools[:0]
	for _, c := range d.VIPPools {
		if _, n, err := net.ParseCIDR(c); err == nil {
			p.pools = append(p.pools, n)
		}
	}
	// A member the feed no longer lists (or lists again as serving after a
	// restart with a new vm_ip) needs no override any more.
	live := map[string]bool{}
	for _, v := range d.VIPs {
		for _, m := range v.Members {
			live[memberKey(v.VIP, m.VMIP)] = true
		}
	}
	for k, o := range p.overrides {
		if !live[k] || time.Now().After(o.until) {
			delete(p.overrides, k)
		}
	}
}

// Desired is the last desired state (nil before the first).
func (p *Programmer) Desired() *hostapi.DirectorDesired {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.desired
}

// ours reports whether a kernel service is this director's to manage: inside
// a VIP pool, or one it created. Anything else on the node is left alone.
func (p *Programmer) ours(s ipvs.Service) bool {
	if p.owned[s.Key()] {
		return true
	}
	for _, n := range p.pools {
		if n.Contains(s.Address) {
			return true
		}
	}
	return false
}

// Apply makes the kernel table match desired state (plus live overrides).
// Every step is independent: one failure never stops the rest.
func (p *Programmer) Apply() (Changes, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	var ch Changes
	if p.desired == nil {
		return ch, nil
	}
	now := time.Now()
	want := map[string]ipvs.Service{}
	wantDests := map[string]map[string]ipvs.Destination{}
	for _, v := range p.desired.VIPs {
		if net.ParseIP(v.VIP).To4() == nil {
			continue
		}
		sched := v.Scheduler
		if sched == "" {
			sched = "rr"
		}
		for _, port := range v.Ports {
			s := service(v.VIP, port, sched)
			want[s.Key()] = s
			ds := map[string]ipvs.Destination{}
			for _, m := range v.Members {
				if net.ParseIP(m.VMIP).To4() == nil {
					continue
				}
				w := m.Weight
				if w < 0 {
					w = 0
				}
				if o, ok := p.overrides[memberKey(v.VIP, m.VMIP)]; ok && now.Before(o.until) {
					if o.deleted {
						continue
					}
					w = o.weight
				}
				d := dest(m.VMIP, port, w)
				ds[d.Key()] = d
			}
			wantDests[s.Key()] = ds
		}
	}
	have, err := p.be.Services()
	if err != nil {
		return ch, fmt.Errorf("list services: %w", err)
	}
	haveSvc := map[string]ipvs.Service{}
	for _, s := range have {
		haveSvc[s.Key()] = s
	}
	// Services to remove (ours, not wanted).
	for k, s := range haveSvc {
		if _, ok := want[k]; ok || !p.ours(s) {
			continue
		}
		if err := p.be.DelService(s); err != nil {
			ch.Errors++
			p.logf("del service %s: %v", k, err)
			continue
		}
		delete(p.owned, k)
		ch.ServicesRemoved++
	}
	keys := make([]string, 0, len(want))
	for k := range want {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		s := want[k]
		cur, exists := haveSvc[k]
		switch {
		case !exists:
			if err := p.be.AddService(s); err != nil {
				ch.Errors++
				p.logf("add service %s: %v", k, err)
				continue
			}
			ch.ServicesAdded++
		case cur.Scheduler != s.Scheduler || cur.Flags != s.Flags:
			if err := p.be.UpdateService(s); err != nil {
				ch.Errors++
				p.logf("update service %s: %v", k, err)
				continue
			}
			ch.ServicesUpdated++
		}
		p.owned[k] = true
		// Real servers.
		var haveD []ipvs.Destination
		if exists {
			haveD, err = p.be.Destinations(s)
			if err != nil {
				ch.Errors++
				p.logf("list dests %s: %v", k, err)
				continue
			}
		}
		haveDest := map[string]ipvs.Destination{}
		for _, d := range haveD {
			haveDest[d.Key()] = d
		}
		for dk, d := range haveDest {
			if _, ok := wantDests[k][dk]; ok {
				continue
			}
			if err := p.be.DelDestination(s, d); err != nil {
				ch.Errors++
				p.logf("del rs %s %s: %v", k, dk, err)
				continue
			}
			ch.DestsRemoved++
		}
		dkeys := make([]string, 0, len(wantDests[k]))
		for dk := range wantDests[k] {
			dkeys = append(dkeys, dk)
		}
		sort.Strings(dkeys)
		for _, dk := range dkeys {
			d := wantDests[k][dk]
			cur, ok := haveDest[dk]
			switch {
			case !ok:
				if err := p.be.AddDestination(s, d); err != nil {
					ch.Errors++
					p.logf("add rs %s %s: %v", k, dk, err)
					continue
				}
				ch.DestsAdded++
			case cur.Weight != d.Weight || cur.Forward&0x7 != ipvs.FwdTunnel || cur.TunType != ipvs.TunGRE:
				if err := p.be.UpdateDestination(s, d); err != nil {
					ch.Errors++
					p.logf("update rs %s %s: %v", k, dk, err)
					continue
				}
				ch.DestsUpdated++
			}
		}
	}
	return ch, nil
}

// Withdraw applies a host's push at once: weight 0 (drain: existing
// connections finish, new ones go elsewhere) or delete (confirmed death:
// with expire_nodest_conn=1 the kernel resets that server's connections so
// clients reconnect elsewhere). It returns how many real servers changed.
// Ports empty = every port the VIP serves.
func (p *Programmer) Withdraw(vip, vmIP string, ports []int, weight int, remove bool) (int, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if net.ParseIP(vip).To4() == nil || net.ParseIP(vmIP).To4() == nil {
		return 0, fmt.Errorf("vip %q / vm_ip %q: IPv4 required", vip, vmIP)
	}
	if weight < 0 {
		weight = 0
	}
	p.overrides[memberKey(vip, vmIP)] = override{weight: weight, deleted: remove, until: time.Now().Add(OverrideTTL)}
	if len(ports) == 0 {
		ports = p.portsOf(vip)
	}
	n := 0
	var firstErr error
	for _, port := range ports {
		s := service(vip, port, "")
		d := dest(vmIP, port, weight)
		var err error
		if remove {
			err = p.be.DelDestination(s, d)
		} else {
			err = p.be.UpdateDestination(s, d)
			if err != nil && !p.hasDest(s, d) {
				err = nil // already gone (health check or an earlier push): a drain is idempotent
			}
		}
		if err != nil {
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		n++
	}
	return n, firstErr
}

// hasDest reports whether the kernel still has this real server (locked).
func (p *Programmer) hasDest(s ipvs.Service, d ipvs.Destination) bool {
	ds, err := p.be.Destinations(s)
	if err != nil {
		return false
	}
	for _, x := range ds {
		if x.Key() == d.Key() {
			return true
		}
	}
	return false
}

// Register applies a host's registration push (a replica turned serving):
// the member is added, or re-weighted, ahead of the desired feed. Unknown
// VIPs (no service yet) are refused: the feed creates services.
func (p *Programmer) Register(vip, vmIP string, ports []int, weight int) (int, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if net.ParseIP(vip).To4() == nil || net.ParseIP(vmIP).To4() == nil {
		return 0, fmt.Errorf("vip %q / vm_ip %q: IPv4 required", vip, vmIP)
	}
	delete(p.overrides, memberKey(vip, vmIP))
	if len(ports) == 0 {
		ports = p.portsOf(vip)
	}
	if len(ports) == 0 {
		return 0, fmt.Errorf("vip %s has no service here yet", vip)
	}
	n := 0
	var firstErr error
	for _, port := range ports {
		s := service(vip, port, "")
		d := dest(vmIP, port, weight)
		err := p.be.UpdateDestination(s, d)
		if err != nil {
			err = p.be.AddDestination(s, d)
		}
		if err != nil {
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		n++
	}
	return n, firstErr
}

// portsOf lists the desired ports of a VIP (locked).
func (p *Programmer) portsOf(vip string) []int {
	if p.desired == nil {
		return nil
	}
	for _, v := range p.desired.VIPs {
		if v.VIP == vip {
			return v.Ports
		}
	}
	return nil
}

// Overrides is a snapshot (metrics and tests).
func (p *Programmer) Overrides() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.overrides)
}

// Counts reports services and real servers this director owns.
func (p *Programmer) Counts() (services, dests int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	svcs, err := p.be.Services()
	if err != nil {
		return 0, 0
	}
	for _, s := range svcs {
		if !p.ours(s) {
			continue
		}
		services++
		if ds, err := p.be.Destinations(s); err == nil {
			dests += len(ds)
		}
	}
	return services, dests
}

// Scheduler picks rr unless the members' slots differ (then wlc). The
// control plane sends the choice; this is the shared rule for both sides.
func Scheduler(members []hostapi.DirectorMember) string {
	first := -1
	for _, m := range members {
		if first == -1 {
			first = m.Slots
		} else if m.Slots != first {
			return "wlc"
		}
	}
	return "rr"
}
