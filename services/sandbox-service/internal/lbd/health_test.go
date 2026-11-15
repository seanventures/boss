package lbd

import (
	"context"
	"errors"
	"sync"
	"testing"

	"github.com/seanheiney/boss/services/sandbox-service/internal/lbd/ipvs"
)

func TestHealthCheckDrainsAfterTwoFailuresDeletesAfterFourAndRestores(t *testing.T) {
	be := ipvs.NewFake()
	prog := NewProgrammer(be, t.Logf)
	prog.SetDesired(desired(1))
	_, _ = prog.Apply()
	var mu sync.Mutex
	dead := map[string]bool{"10.200.8.3": true}
	probes := 0
	prober := FuncProber(func(_ context.Context, vmIP, vip string, port int) error {
		mu.Lock()
		defer mu.Unlock()
		probes++
		if vip != "203.0.113.17" || port != 443 {
			t.Errorf("probe %s %d", vip, port)
		}
		if dead[vmIP] {
			return errors.New("no SYN-ACK")
		}
		return nil
	})
	m := NewMetrics()
	c := &Checker{Prober: prober, Prog: prog, Metrics: m, Logf: t.Logf}
	ctx := context.Background()
	c.Tick(ctx) // 1 failure: nothing yet
	if be.Table()["tcp/203.0.113.17:443"]["10.200.8.3:443"] != 1 {
		t.Fatal("withdrawn after one failure")
	}
	c.Tick(ctx) // 2 failures: weight 0
	if be.Table()["tcp/203.0.113.17:443"]["10.200.8.3:443"] != 0 {
		t.Fatalf("not drained after two failures: %v", be.Table())
	}
	if len(c.Down()) != 1 {
		t.Fatalf("down: %v", c.Down())
	}
	c.Tick(ctx)
	c.Tick(ctx) // 4 failures: deleted
	// The feed still lists it (the host believes it serves): it stays out.
	prog.SetDesired(desired(2))
	_, _ = prog.Apply()
	// The feed drops the member and lists it again between two ticks: still out.
	d := desired(3)
	d.VIPs[0].Members = d.VIPs[0].Members[:1]
	prog.SetDesired(d)
	prog.SetDesired(desired(4))
	_, _ = prog.Apply()
	if _, ok := be.Table()["tcp/203.0.113.17:443"]["10.200.8.3:443"]; ok {
		t.Fatal("a feed drop/re-add between ticks brought a dead member back")
	}
	if _, ok := be.Table()["tcp/203.0.113.17:443"]["10.200.8.3:443"]; ok {
		t.Fatal("not deleted after four failures")
	}
	// The healthy members were never touched.
	if be.Table()["tcp/203.0.113.17:443"]["10.200.4.7:443"] != 1 {
		t.Fatal("a healthy member was changed")
	}
	// Recovery: two successes restore it.
	mu.Lock()
	dead["10.200.8.3"] = false
	mu.Unlock()
	c.Tick(ctx)
	if _, ok := be.Table()["tcp/203.0.113.17:443"]["10.200.8.3:443"]; ok {
		t.Fatal("restored after one success")
	}
	c.Tick(ctx)
	if be.Table()["tcp/203.0.113.17:443"]["10.200.8.3:443"] != 1 || len(c.Down()) != 0 {
		t.Fatalf("not restored: %v down=%v", be.Table(), c.Down())
	}
	mu.Lock()
	n := probes
	mu.Unlock()
	if n != 6*3 {
		t.Fatalf("expected one probe per member per tick, got %d", n)
	}
	if _, ok := m.Last("boss_lbd_withdraw_seconds"); !ok {
		t.Fatal("no withdraw latency observed")
	}
}
