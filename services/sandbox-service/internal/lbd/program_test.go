package lbd

import (
	"strings"
	"testing"
	"time"

	"github.com/seanventures/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanventures/boss/services/sandbox-service/internal/lbd/ipvs"
)

func desired(gen int64) *hostapi.DirectorDesired {
	return &hostapi.DirectorDesired{Changed: true, DirectorID: "dir_a", Generation: gen,
		VIPPools: []string{"203.0.113.0/26"},
		VIPs: []hostapi.DirectorVIP{{VIP: "203.0.113.17", Ports: []int{443}, Scheduler: "rr", Group: 0,
			Members: []hostapi.DirectorMember{
				{ReplicaID: "rep_1", VMIP: "10.200.4.7", HostID: "hst_1", Weight: 1, Slots: 4},
				{ReplicaID: "rep_2", VMIP: "10.200.8.3", HostID: "hst_2", Weight: 1, Slots: 4},
				{ReplicaID: "rep_3", VMIP: "10.200.4.9", HostID: "hst_1", Weight: 1, Slots: 4}}}}}
}

func TestApplyProgramsGRETunnelRealServersAndIsIdempotent(t *testing.T) {
	be := ipvs.NewFake()
	p := NewProgrammer(be, t.Logf)
	p.SetDesired(desired(1))
	ch, err := p.Apply()
	if err != nil {
		t.Fatal(err)
	}
	if ch.ServicesAdded != 1 || ch.DestsAdded != 3 || ch.Errors != 0 {
		t.Fatalf("first apply: %s", ch)
	}
	tbl := be.Table()
	if len(tbl["tcp/203.0.113.17:443"]) != 3 || tbl["tcp/203.0.113.17:443"]["10.200.8.3:443"] != 1 {
		t.Fatalf("table: %v", tbl)
	}
	for _, op := range be.Ops {
		if strings.HasPrefix(op, "add-dest") && !strings.Contains(op, "fwd=2 tun=2") {
			t.Fatalf("real server not GRE tunnel mode: %s", op)
		}
	}
	// Persistence off: flags 0.
	svcs, _ := be.Services()
	if svcs[0].Flags != 0 || svcs[0].Scheduler != "rr" {
		t.Fatalf("service %+v", svcs[0])
	}
	n := len(be.Ops)
	if ch, _ = p.Apply(); ch.ServicesAdded+ch.DestsAdded+ch.DestsUpdated+ch.DestsRemoved != 0 || len(be.Ops) != n {
		t.Fatalf("second apply not idempotent: %s ops=%v", ch, be.Ops[n:])
	}
}

func TestApplyFollowsDesiredChangesAndLeavesForeignServicesAlone(t *testing.T) {
	be := ipvs.NewFake()
	_ = be.AddService(ipvs.Service{Address: parse("192.0.2.9"), Port: 80, Protocol: 6, Scheduler: "rr"}) // not in a pool
	_ = be.AddService(ipvs.Service{Address: parse("203.0.113.40"), Port: 443, Protocol: 6, Scheduler: "rr"})
	p := NewProgrammer(be, t.Logf)
	p.SetDesired(desired(1))
	if _, err := p.Apply(); err != nil {
		t.Fatal(err)
	}
	tbl := be.Table()
	if _, ok := tbl["tcp/192.0.2.9:80"]; !ok {
		t.Fatal("a service outside the VIP pools was removed")
	}
	if _, ok := tbl["tcp/203.0.113.40:443"]; ok {
		t.Fatal("a stale service inside the VIP pool survived")
	}
	// A member leaves, one changes weight, the scheduler switches to wlc.
	d := desired(2)
	d.VIPs[0].Members = d.VIPs[0].Members[:2]
	d.VIPs[0].Members[1].Weight = 3
	d.VIPs[0].Scheduler = "wlc"
	p.SetDesired(d)
	ch, _ := p.Apply()
	if ch.DestsRemoved != 1 || ch.DestsUpdated != 1 || ch.ServicesUpdated != 1 {
		t.Fatalf("changes: %s", ch)
	}
	tbl = be.Table()
	if len(tbl["tcp/203.0.113.17:443"]) != 2 || tbl["tcp/203.0.113.17:443"]["10.200.8.3:443"] != 3 {
		t.Fatalf("table: %v", tbl)
	}
	// The VIP goes away: so does the service.
	p.SetDesired(&hostapi.DirectorDesired{Generation: 3, VIPPools: []string{"203.0.113.0/26"}})
	ch, _ = p.Apply()
	if ch.ServicesRemoved != 1 || len(be.Table()) != 1 {
		t.Fatalf("remove: %s %v", ch, be.Table())
	}
}

func TestWithdrawAppliesAtOnceAndOutlivesAStaleFeed(t *testing.T) {
	be := ipvs.NewFake()
	p := NewProgrammer(be, t.Logf)
	p.SetDesired(desired(1))
	_, _ = p.Apply()
	n, err := p.Withdraw("203.0.113.17", "10.200.8.3", nil, 0, false, 0)
	if err != nil || n != 1 {
		t.Fatalf("withdraw: n=%d err=%v", n, err)
	}
	if be.Table()["tcp/203.0.113.17:443"]["10.200.8.3:443"] != 0 {
		t.Fatal("weight not 0 after the push")
	}
	// The desired feed still lists the member at weight 1 (it has not seen
	// the death yet): the override wins.
	p.SetDesired(desired(2))
	_, _ = p.Apply()
	if be.Table()["tcp/203.0.113.17:443"]["10.200.8.3:443"] != 0 {
		t.Fatal("a stale feed re-raised the weight")
	}
	// Confirmed death: delete; the feed (still stale) must not re-add it.
	if n, err := p.Withdraw("203.0.113.17", "10.200.8.3", nil, 0, true, 0); err != nil || n != 1 {
		t.Fatalf("delete: n=%d err=%v", n, err)
	}
	_, _ = p.Apply()
	if _, ok := be.Table()["tcp/203.0.113.17:443"]["10.200.8.3:443"]; ok {
		t.Fatal("deleted real server re-added from a stale feed")
	}
	// The feed drops the member: the override is forgotten; a later feed that
	// lists it again (the VM came back) re-adds it.
	d := desired(3)
	d.VIPs[0].Members = append(d.VIPs[0].Members[:1], d.VIPs[0].Members[2:]...)
	p.SetDesired(d)
	_, _ = p.Apply()
	if p.Overrides() != 0 {
		t.Fatalf("override kept after the feed dropped the member: %d", p.Overrides())
	}
	p.SetDesired(desired(4))
	_, _ = p.Apply()
	if be.Table()["tcp/203.0.113.17:443"]["10.200.8.3:443"] != 1 {
		t.Fatal("returned member not re-added")
	}
}

func TestRegisterPushAddsAheadOfTheFeedAndClearsAnOverride(t *testing.T) {
	be := ipvs.NewFake()
	p := NewProgrammer(be, t.Logf)
	d := desired(1)
	d.VIPs[0].Members = d.VIPs[0].Members[:1]
	p.SetDesired(d)
	_, _ = p.Apply()
	// A member the desired state does not list can never be added by a push.
	if _, err := p.Register("203.0.113.17", "10.200.8.3", nil, 2, 0); err != ErrUnknownMember {
		t.Fatalf("unknown member registered: %v", err)
	}
	p.SetDesired(desired(2))
	_, _ = p.Withdraw("203.0.113.17", "10.200.8.3", nil, 0, true, 0) // out (deleted) ahead of the feed
	if n, err := p.Register("203.0.113.17", "10.200.8.3", nil, 2, 0); err != nil || n != 1 {
		t.Fatalf("register: n=%d err=%v", n, err)
	}
	if be.Table()["tcp/203.0.113.17:443"]["10.200.8.3:443"] != 2 {
		t.Fatalf("table: %v", be.Table())
	}
	if _, err := p.Register("203.0.113.99", "10.200.8.3", nil, 1, 0); err == nil {
		t.Fatal("a registration for an unknown VIP must be refused (the feed creates services)")
	}
	_, _ = p.Withdraw("203.0.113.17", "10.200.4.7", nil, 0, false, 0)
	if n, _ := p.Register("203.0.113.17", "10.200.4.7", nil, 1, 0); n != 1 || p.Overrides() != 0 {
		t.Fatalf("register did not clear the override: n=%d overrides=%d", n, p.Overrides())
	}
}

func TestOverrideExpires(t *testing.T) {
	be := ipvs.NewFake()
	p := NewProgrammer(be, t.Logf)
	p.SetDesired(desired(1))
	_, _ = p.Apply()
	_, _ = p.Withdraw("203.0.113.17", "10.200.8.3", nil, 0, false, 0)
	p.mu.Lock()
	o := p.overrides[memberKey("203.0.113.17", "10.200.8.3")]
	o.until = time.Now().Add(-time.Second)
	p.overrides[memberKey("203.0.113.17", "10.200.8.3")] = o
	p.mu.Unlock()
	_, _ = p.Apply()
	if be.Table()["tcp/203.0.113.17:443"]["10.200.8.3:443"] != 1 {
		t.Fatal("an expired override still held the weight at 0")
	}
}

func TestSchedulerRule(t *testing.T) {
	same := []hostapi.DirectorMember{{Slots: 4}, {Slots: 4}}
	diff := []hostapi.DirectorMember{{Slots: 4}, {Slots: 8}}
	if Scheduler(same) != "rr" || Scheduler(diff) != "wlc" || Scheduler(nil) != "rr" {
		t.Fatal("rr unless slots differ")
	}
}

func TestTheLatestPushWinsPerMember(t *testing.T) {
	be := ipvs.NewFake()
	p := NewProgrammer(be, t.Logf)
	p.SetDesired(desired(1))
	_, _ = p.Apply()
	// The host decided: withdraw (seq 10), then register (seq 11). They cross
	// on the network: the register lands first, the withdraw second.
	if n, err := p.Register("203.0.113.17", "10.200.8.3", nil, 3, 11); err != nil || n != 1 {
		t.Fatalf("register: %d %v", n, err)
	}
	if _, err := p.Withdraw("203.0.113.17", "10.200.8.3", nil, 0, false, 10); err != ErrStale {
		t.Fatalf("an older withdraw was applied over a newer register: %v", err)
	}
	if be.Table()["tcp/203.0.113.17:443"]["10.200.8.3:443"] != 3 {
		t.Fatalf("latest decision lost: %v", be.Table())
	}
	if _, err := p.Withdraw("203.0.113.17", "10.200.8.3", nil, 0, true, 12); err != nil {
		t.Fatal(err)
	}
	if _, err := p.Register("203.0.113.17", "10.200.8.3", nil, 1, 11); err != ErrStale {
		t.Fatal("a stale register re-added a deleted member")
	}
}

func TestAHealthDeadMemberStaysOutPastTheOverrideTTL(t *testing.T) {
	be := ipvs.NewFake()
	p := NewProgrammer(be, t.Logf)
	p.SetDesired(desired(1))
	_, _ = p.Apply()
	if _, err := p.SetHealth("203.0.113.17", "10.200.8.3", true); err != nil {
		t.Fatal(err)
	}
	// Long after any push override would have expired, the feed (which still
	// says serving: the host thinks the VM is fine) cannot bring it back.
	p.mu.Lock()
	for k, o := range p.overrides {
		o.until = time.Now().Add(-time.Hour)
		p.overrides[k] = o
	}
	p.mu.Unlock()
	p.SetDesired(desired(2))
	_, _ = p.Apply()
	if _, ok := be.Table()["tcp/203.0.113.17:443"]["10.200.8.3:443"]; ok {
		t.Fatal("a health-dead member returned to rotation")
	}
	if n, _ := p.Register("203.0.113.17", "10.200.8.3", nil, 1, 0); n != 0 {
		t.Fatal("a registration overrode the health checks")
	}
	p.ClearHealth("203.0.113.17", "10.200.8.3")
	_, _ = p.Apply()
	if be.Table()["tcp/203.0.113.17:443"]["10.200.8.3:443"] != 1 {
		t.Fatal("not restored after the checks passed")
	}
}
