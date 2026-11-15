package lbd

import (
	"fmt"
	"net"
	"path/filepath"
	"strings"
	"testing"
)

func parse(s string) net.IP { return net.ParseIP(s).To4() }

func TestGroupingSplitsVIPsInFiftiesAndAlternatesMasters(t *testing.T) {
	var vips []string
	for i := 0; i < 120; i++ {
		vips = append(vips, fmt.Sprintf("203.0.113.%d", i))
	}
	gs := Group(vips, []string{"dir_b", "dir_a"})
	if len(gs) != 3 || len(gs[0].VIPs) != 50 || len(gs[2].VIPs) != 20 {
		t.Fatalf("groups: %d", len(gs))
	}
	if gs[0].Master != "dir_a" || gs[1].Master != "dir_b" || gs[2].Master != "dir_a" {
		t.Fatalf("masters: %s %s %s", gs[0].Master, gs[1].Master, gs[2].Master)
	}
	if gs[0].VRID != 1 || gs[2].VRID != 3 {
		t.Fatalf("vrids: %d %d", gs[0].VRID, gs[2].VRID)
	}
	// Deterministic whatever the input order.
	again := Group(append([]string{vips[119]}, vips[:119]...), []string{"dir_a", "dir_b"})
	if strings.Join(again[0].VIPs, ",") != strings.Join(gs[0].VIPs, ",") {
		t.Fatal("grouping depends on input order")
	}
}

func TestKeepalivedConfigIsVRRPOnly(t *testing.T) {
	gs := Group([]string{"203.0.113.17", "203.0.113.18", "203.0.113.19"}, []string{"dir_a", "dir_b"})
	gs = append(gs, hostapiGroup{Group: 1, VRID: 2, Master: "dir_b", VIPs: []string{"203.0.113.40"}})
	conf := RenderKeepalived(KeepalivedConfig{SelfID: "dir_a", SelfAddress: "192.0.2.10", PeerAddress: "192.0.2.11",
		Iface: "eth0", Groups: gs, TrackBin: "/usr/local/bin/boss-lbd", StateDir: "/run/boss-lbd"})
	for _, want := range []string{
		"vrrp_instance boss_g0", "virtual_router_id 1", "priority 150", // MASTER side of group 0
		"vrrp_instance boss_g1", "virtual_router_id 2", "priority 100", // BACKUP side of group 1
		"nopreempt", "unicast_src_ip 192.0.2.10", "    192.0.2.11\n", "state BACKUP",
		"virtual_ipaddress {\n    203.0.113.17/32 dev eth0", "virtual_ipaddress_excluded {\n    203.0.113.18/32 dev eth0\n    203.0.113.19/32 dev eth0",
		"vrrp_script boss_lease_g0", "boss-lbd track --group 0 --state-dir /run/boss-lbd",
		"notify_master \"/usr/local/bin/boss-lbd notify --group 0 --state-dir /run/boss-lbd --to MASTER\"",
	} {
		if !strings.Contains(conf, want) {
			t.Fatalf("missing %q in:\n%s", want, conf)
		}
	}
	if strings.Contains(conf, "virtual_server") || strings.Contains(conf, "real_server") {
		t.Fatal("keepalived must never own IPVS (D3)")
	}
	// A single director (no peer yet) is MASTER of everything.
	solo := RenderKeepalived(KeepalivedConfig{SelfID: "dir_a", SelfAddress: "192.0.2.10", Iface: "eth0", Groups: gs})
	if strings.Count(solo, "priority 150") != 2 || strings.Contains(solo, "unicast_peer") {
		t.Fatalf("solo config:\n%s", solo)
	}
}

func TestTrackFaultsOnlyAMasterWhoseLeaseIsAnothers(t *testing.T) {
	leases := map[string]string{"0": "dir_a", "1": "dir_b", "2": ""}
	f := TrackDecision("dir_a", []int{0, 1, 2}, leases)
	if f[0] || !f[1] || f[2] {
		t.Fatalf("decision %v", f)
	}
	// BACKUP of a group another holds: never a fault.
	if TrackDecision("dir_a", []int{0}, leases)[1] {
		t.Fatal("a BACKUP faulted")
	}
	dir := t.TempDir()
	if err := WriteTrack(dir, f); err != nil {
		t.Fatal(err)
	}
	if Track(dir, 0) != 0 || Track(dir, 1) != 1 || Track(dir, 2) != 0 || Track(dir, 7) != 0 {
		t.Fatal("track exit codes")
	}
	_ = WriteNotify(dir, 1, "master")
	_ = WriteNotify(dir, 0, "BACKUP")
	if ms := Masters(ReadStates(dir)); len(ms) != 1 || ms[0] != 1 {
		t.Fatalf("masters %v", ms)
	}
	if _, err := WriteIfChanged(filepath.Join(dir, "k.conf"), "x"); err != nil {
		t.Fatal(err)
	}
	if ch, _ := WriteIfChanged(filepath.Join(dir, "k.conf"), "x"); ch {
		t.Fatal("unchanged content reported as changed")
	}
}
