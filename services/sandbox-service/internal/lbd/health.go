package lbd

import (
	"context"
	"sort"
	"sync"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
)

// Health-check policy (LLD §3.14.2): 1 s interval, 2 failures → withdraw
// (weight 0), 2 more → delete (the host is presumed dead; its connections
// are reset). 2 successes → restored. Checks run through the real data path:
// a TCP SYN inside GRE to the replica's vm_ip with the VIP as inner
// destination, answered by the VM's SYN-ACK straight back (DSR), so they
// prove GRE decap, the VIP on the VM and the listener, not just reachability.
const (
	CheckInterval  = time.Second
	CheckTimeout   = 700 * time.Millisecond
	FailsToDrain   = 2
	FailsToDelete  = 4
	RisesToRestore = 2
)

// Prober answers one check: nil = the replica accepted a SYN on vip:port.
type Prober interface {
	Probe(ctx context.Context, vmIP, vip string, port int) error
}

type memberHealth struct {
	fails, rises int
	withdrawn    bool // weight 0 pushed
	deleted      bool
	lastErr      string
}

// Checker runs the health checks against desired members.
type Checker struct {
	Prober  Prober
	Prog    *Programmer
	Metrics *Metrics
	Logf    func(string, ...any)
	// Parallel bounds concurrent probes per tick (default 64).
	Parallel int

	mu    sync.Mutex
	state map[string]*memberHealth // "<vip>|<vm_ip>"
}

// Tick runs one round of checks over the current desired state.
func (c *Checker) Tick(ctx context.Context) {
	d := c.Prog.Desired()
	if d == nil || c.Prober == nil {
		return
	}
	if c.state == nil {
		c.state = map[string]*memberHealth{}
	}
	type target struct {
		vip, vmIP string
		port      int
		key       string
	}
	var targets []target
	live := map[string]bool{}
	for _, v := range d.VIPs {
		if len(v.Ports) == 0 || len(v.Members) == 0 {
			continue
		}
		port := v.Ports[0]
		for _, m := range v.Members {
			k := memberKey(v.VIP, m.VMIP)
			live[k] = true
			targets = append(targets, target{v.VIP, m.VMIP, port, k})
		}
	}
	c.mu.Lock()
	for k := range c.state {
		if !live[k] {
			delete(c.state, k)
		}
	}
	c.mu.Unlock()
	par := c.Parallel
	if par <= 0 {
		par = 64
	}
	sem := make(chan struct{}, par)
	var wg sync.WaitGroup
	for _, t := range targets {
		wg.Add(1)
		sem <- struct{}{}
		go func(t target) {
			defer wg.Done()
			defer func() { <-sem }()
			pctx, cancel := context.WithTimeout(ctx, CheckTimeout)
			err := c.Prober.Probe(pctx, t.vmIP, t.vip, t.port)
			cancel()
			c.record(t.vip, t.vmIP, t.key, err)
		}(t)
	}
	wg.Wait()
	c.gauges()
}

func (c *Checker) record(vip, vmIP, key string, err error) {
	c.mu.Lock()
	h := c.state[key]
	if h == nil {
		h = &memberHealth{}
		c.state[key] = h
	}
	var act string
	if err != nil {
		h.rises = 0
		h.fails++
		h.lastErr = err.Error()
		switch {
		case h.fails == FailsToDrain && !h.withdrawn:
			h.withdrawn = true
			act = "drain"
		case h.fails >= FailsToDelete && !h.deleted:
			h.deleted = true
			act = "delete"
		}
	} else {
		h.fails = 0
		h.rises++
		if (h.withdrawn || h.deleted) && h.rises >= RisesToRestore {
			h.withdrawn, h.deleted = false, false
			act = "restore"
		}
	}
	c.mu.Unlock()
	if act == "" {
		return
	}
	start := time.Now()
	var n int
	var perr error
	switch act {
	case "drain":
		n, perr = c.Prog.SetHealth(vip, vmIP, false)
	case "delete":
		n, perr = c.Prog.SetHealth(vip, vmIP, true)
	case "restore":
		c.Prog.ClearHealth(vip, vmIP)
		_, perr = c.Prog.Apply()
	}
	if c.Metrics != nil {
		c.Metrics.Inc("boss_lbd_healthcheck_"+act+"_total", 1)
		if act != "restore" {
			c.Metrics.Observe("boss_lbd_withdraw_seconds", time.Since(start))
		}
	}
	if c.Logf != nil {
		c.Logf("health %s: vip %s vm_ip %s (%d rs, err=%v)", act, vip, vmIP, n, perr)
	}
}

func (c *Checker) gauges() {
	if c.Metrics == nil {
		return
	}
	c.mu.Lock()
	var down int
	for _, h := range c.state {
		if h.withdrawn || h.deleted {
			down++
		}
	}
	total := len(c.state)
	c.mu.Unlock()
	c.Metrics.Set("boss_lbd_members", float64(total))
	c.Metrics.Set("boss_lbd_members_down", float64(down))
}

// Down lists members currently withdrawn by health (sorted; tests, status).
func (c *Checker) Down() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	var out []string
	for k, h := range c.state {
		if h.withdrawn || h.deleted {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out
}

// ClearOverride forgets a push or health override for a member (restore).
func (p *Programmer) ClearOverride(vip, vmIP string) {
	p.mu.Lock()
	delete(p.overrides, memberKey(vip, vmIP))
	p.mu.Unlock()
}

// FuncProber adapts a function (tests).
type FuncProber func(ctx context.Context, vmIP, vip string, port int) error

func (f FuncProber) Probe(ctx context.Context, vmIP, vip string, port int) error {
	return f(ctx, vmIP, vip, port)
}

var _ = hostapi.DirectorMember{}
