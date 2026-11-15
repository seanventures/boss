package netpol

import (
	"context"
	"strings"
	"testing"
)

func TestTapNameAndMACAreStable(t *testing.T) {
	a, b := TapName("vm-1"), TapName("vm-1")
	if a != b || !ValidTap(a) || len(a) > 15 {
		t.Fatalf("tap %q", a)
	}
	if TapName("vm-2") == a {
		t.Fatal("distinct VMs must get distinct taps")
	}
	if MACFor("10.200.4.7") != "02:fc:0a:c8:04:07" {
		t.Fatal(MACFor("10.200.4.7"))
	}
}

func TestRenderVMIsInboundOnly(t *testing.T) {
	v := VMNet{VMID: "vm-1", Tap: TapName("vm-1"), VMIP: "10.200.4.7", VIP: "203.0.113.17", VIPPorts: []int{8443, 443}, VMPrefix: "10.200.4.0/23"}
	s := RenderVM(v)
	tap := v.Tap
	tuple := "ip saddr . ip daddr . tcp sport . tcp dport"
	must := []string{
		"ip daddr 169.254.169.254 counter name meta_" + tap + " drop",
		"add rule inet boss_hostd out_" + tap + " ip daddr @internal_nets counter name orig_" + tap + " drop",
		"tcp flags & (syn|ack) != syn " + tuple + " @flows_" + tap + " counter name reply_" + tap + " accept",
		"tcp flags & rst == rst " + tuple + " @flows_" + tap + " delete @flows_" + tap,
		"tcp flags & fin == fin " + tuple + " @flows_" + tap + " delete @flows_" + tap + " { " + tuple + " } add @closing_" + tap,
		"tcp flags & (syn|ack) != syn " + tuple + " @closing_" + tap + " counter name reply_" + tap + " accept",
		"add rule inet boss_hostd out_" + tap + " counter name orig_" + tap + " drop",
		"ip saddr @directors ip daddr 10.200.4.7 gre ip daddr 203.0.113.17 gre tcp dport { 443, 8443 } gre tcp flags & (syn|ack) == syn update @flows_" + tap + " { gre ip daddr . gre ip saddr . gre tcp dport . gre tcp sport }",
		"add rule inet boss_hostd in_" + tap + " counter name indrop_" + tap + " drop",
	}
	for _, m := range must {
		if !strings.Contains(s, m) {
			t.Fatalf("missing %q in:\n%s", m, s)
		}
	}
	// internal nets are dropped BEFORE any reply accept.
	if strings.Index(s, "@internal_nets") > strings.Index(s, "accept") {
		t.Fatal("the internal-net drop must precede the reply accepts")
	}
	v.VIP = ""
	if strings.Contains(RenderVM(v), "accept") {
		t.Fatal("without a VIP nothing may leave or enter the VM")
	}
	base := RenderBase(Policy{VMPrefix: "10.200.4.0/23"}.Internal())
	if !strings.Contains(base, `iifname "bt*" counter name drop_unknown_tap drop`) || !strings.Contains(base, "169.254.0.0/16") ||
		!strings.Contains(base, "10.200.4.0/23") {
		t.Fatalf("base: %s", base)
	}
}

// The helper's own policy decides prefix and owner; a caller cannot widen it.
func TestPolicyOverridesTheCaller(t *testing.T) {
	p := Policy{VMPrefix: "10.200.4.0/23", TapOwnerUID: 1000}
	ok := VMNet{Tap: TapName("x"), VMIP: "10.200.4.7", OwnerUID: 1000}
	if err := p.Check(&ok); err != nil || ok.VMPrefix != "10.200.4.0/23" {
		t.Fatalf("%v %+v", err, ok)
	}
	for _, bad := range []VMNet{
		{Tap: TapName("x"), VMIP: "10.9.0.7", VMPrefix: "10.9.0.0/16", OwnerUID: 1000}, // caller widens the prefix
		{Tap: TapName("x"), VMIP: "10.9.0.7", OwnerUID: 1000},                          // outside the helper's prefix
		{Tap: TapName("x"), VMIP: "10.200.4.7", OwnerUID: 0},                           // tap for root
	} {
		b := bad
		if p.Check(&b) == nil {
			t.Fatalf("accepted %+v", bad)
		}
	}
	if (Policy{}).Check(&ok) == nil {
		t.Fatal("a helper without --vm-prefix must refuse everything")
	}
}

func TestValidateIsTheHelperBoundary(t *testing.T) {
	good := VMNet{Tap: TapName("x"), VMIP: "10.200.4.7", VMPrefix: "10.200.4.0/23"}
	if err := good.Validate(); err != nil {
		t.Fatal(err)
	}
	bad := []VMNet{
		{Tap: "eth0", VMIP: "10.200.4.7", VMPrefix: "10.200.4.0/23"},
		{Tap: TapName("x"), VMIP: "10.9.9.9", VMPrefix: "10.200.4.0/23"},
		{Tap: TapName("x"), VMIP: "10.200.4.7", VMPrefix: "10.200.4.0/23", VIP: "::1"},
		{Tap: TapName("x"), VMIP: "10.200.4.7", VMPrefix: "10.200.4.0/23", VIPPorts: []int{70000}},
		{Tap: "bt; nft flush ruleset", VMIP: "10.200.4.7", VMPrefix: "10.200.4.0/23"},
	}
	for _, b := range bad {
		if b.Validate() == nil {
			t.Fatalf("accepted %+v", b)
		}
	}
	resp := Dispatch(context.Background(), Noop{}, helperReq{Op: "remove", Tap: "eth0"})
	if resp.OK {
		t.Fatal("helper removed a non-boss interface")
	}
	if _, err := RenderDirectors([]string{"1.2.3.4", "x; flush"}); err == nil {
		t.Fatal("director list must be IPv4 only")
	}
	if r := Dispatch(context.Background(), Noop{}, helperReq{Op: "flush_everything"}); r.OK {
		t.Fatal("unknown helper op accepted")
	}
}

// The listing nft 1.0.9 prints on boss-1 for the base chains.
const realListing = `table inet boss_hostd {
	chain pre {
		type filter hook prerouting priority raw; policy accept;
		iifname vmap @from_tap
		iifname "bt*" counter name "drop_unknown_tap" drop
	}
}
table inet boss_hostd {
	chain post {
		type filter hook postrouting priority filter; policy accept;
		oifname vmap @to_tap
		oifname "bt*" counter name "drop_unknown_tap" drop
	}
}`

func TestBaseIntactReadsRealNftOutput(t *testing.T) {
	if ok, why := BaseIntact(realListing); !ok {
		t.Fatal(why)
	}
	drifted := strings.Replace(realListing, `oifname "bt*" counter name "drop_unknown_tap" drop`, "", 1)
	if ok, _ := BaseIntact(drifted); ok {
		t.Fatal("a missing fail-closed rule must read as drift")
	}
}
