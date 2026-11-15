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
	must := []string{
		"ip daddr 169.254.169.254 counter name meta_" + tap + " drop",
		"ip saddr 203.0.113.17 tcp sport { 443, 8443 } tcp flags & (syn|ack) != syn @nh,128,32 @peers_" + tap + " counter name reply_" + tap + " accept",
		"add rule inet boss_hostd out_" + tap + " counter name orig_" + tap + " drop",
		"ip saddr @directors ip daddr 10.200.4.7 meta l4proto 4",
		"add rule inet boss_hostd in_" + tap + " counter name indrop_" + tap + " drop",
		"flush chain inet boss_hostd out_" + tap,
	}
	for _, m := range must {
		if !strings.Contains(s, m) {
			t.Fatalf("missing %q in:\n%s", m, s)
		}
	}
	// The out chain's only accept is the reply rule, and it is last-but-one.
	out := []string{}
	for _, line := range strings.Split(s, "\n") {
		if strings.HasPrefix(line, "add rule inet boss_hostd out_") {
			out = append(out, line)
		}
	}
	if len(out) != 3 || !strings.HasSuffix(out[2], "drop") || strings.Count(strings.Join(out, "\n"), "accept") != 1 {
		t.Fatalf("out chain must be meta-drop, reply-accept, drop-all: %v", out)
	}
	// No VIP: no accept at all on the way out.
	v.VIP = ""
	if strings.Contains(RenderVM(v), "reply_"+tap+" accept") {
		t.Fatal("without a VIP nothing may leave the VM")
	}
	base := RenderBase()
	if !strings.Contains(base, `iifname "bt*" counter name drop_unknown_tap drop`) {
		t.Fatal("base must drop traffic from any tap without its own chain")
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
