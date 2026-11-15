package lbd

import (
	"context"
	"crypto/ecdsa"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
	"github.com/seanheiney/boss/services/sandbox-service/internal/lbd/ipvs"
	"github.com/seanheiney/boss/services/sandbox-service/internal/pki"
)

// leaf issues a certificate under issuer with CN=id and the given EKUs
// (the Python CA's shapes: a host's is client auth only, a director's is
// client + server auth).
func leaf(t *testing.T, ca *pki.CA, id string, ekus ...x509.ExtKeyUsage) (tls.Certificate, *ecdsa.PrivateKey) {
	t.Helper()
	if len(ekus) == 0 {
		ekus = []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}
	}
	key, err := pki.GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{SerialNumber: big.NewInt(time.Now().UnixNano()), Subject: pkix.Name{CommonName: id},
		NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: ekus}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca.Cert, &key.PublicKey, ca.Key)
	if err != nil {
		t.Fatal(err)
	}
	c, _ := x509.ParseCertificate(der)
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key, Leaf: c}, key
}

var dirEKU = []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth, x509.ExtKeyUsageServerAuth}

func startDirector(t *testing.T, ca *pki.CA, id string, prog *Programmer, m *Metrics) hostapi.DirectorEndpoint {
	t.Helper()
	cert, _ := leaf(t, ca, id, dirEKU...)
	pool := x509.NewCertPool()
	pool.AddCert(ca.Cert)
	srv := httptest.NewUnstartedServer((&PushServer{Prog: prog, Metrics: m, Logf: t.Logf}).Handler())
	srv.TLS = ServerTLS(func() *tls.Certificate { return &cert }, pool)
	srv.TLS.Certificates = []tls.Certificate{cert} // httptest would add its own otherwise (no SNI from an IP dial)
	srv.StartTLS()
	t.Cleanup(srv.Close)
	host, portS, _ := net.SplitHostPort(strings.TrimPrefix(srv.URL, "https://"))
	port, _ := strconv.Atoi(portS)
	return hostapi.DirectorEndpoint{ID: id, Address: host, Port: port}
}

func TestHostPushWithdrawsAtEveryDirectorOverMTLS(t *testing.T) {
	ca, err := pki.NewCA("fleet")
	if err != nil {
		t.Fatal(err)
	}
	beA, beB := ipvs.NewFake(), ipvs.NewFake()
	progA, progB := NewProgrammer(beA, t.Logf), NewProgrammer(beB, t.Logf)
	mA := NewMetrics()
	for _, p := range []*Programmer{progA, progB} {
		p.SetDesired(desired(1))
		if _, err := p.Apply(); err != nil {
			t.Fatal(err)
		}
	}
	epA := startDirector(t, ca, "dir_a", progA, mA)
	epB := startDirector(t, ca, "dir_b", progB, nil)
	hostCert, _ := leaf(t, ca, "hst_2") // rep_2 / 10.200.8.3 is hst_2's
	pool := x509.NewCertPool()
	pool.AddCert(ca.Cert)
	pusher := &Pusher{Cert: func() *tls.Certificate { return &hostCert }, FleetCA: pool, Timeout: 2 * time.Second}

	start := time.Now()
	res := pusher.Push(context.Background(), []hostapi.DirectorEndpoint{epA, epB}, http.MethodPut, "203.0.113.17", "rep_2",
		hostapi.VIPMember{VMIP: "10.200.8.3", Weight: 0, Reason: "process_exit"})
	for _, r := range res {
		if r.Err != nil {
			t.Fatalf("push to %s: %v", r.Director, r.Err)
		}
	}
	if d := time.Since(start); d > time.Second {
		t.Fatalf("two parallel pushes took %s", d)
	}
	for name, be := range map[string]*ipvs.Fake{"A": beA, "B": beB} {
		if be.Table()["tcp/203.0.113.17:443"]["10.200.8.3:443"] != 0 {
			t.Fatalf("director %s did not drain the member: %v", name, be.Table())
		}
	}
	if last, ok := mA.Last("boss_lbd_withdraw_seconds"); !ok || last > 100*time.Millisecond {
		t.Fatalf("withdraw latency metric: %v %v", last, ok)
	}
	// Confirmed death: DELETE.
	res = pusher.Push(context.Background(), []hostapi.DirectorEndpoint{epA, epB}, http.MethodDelete, "203.0.113.17", "rep_2",
		hostapi.VIPMember{VMIP: "10.200.8.3", Reason: "process_exit"})
	for _, r := range res {
		if r.Err != nil {
			t.Fatalf("delete at %s: %v", r.Director, r.Err)
		}
	}
	if _, ok := beA.Table()["tcp/203.0.113.17:443"]["10.200.8.3:443"]; ok {
		t.Fatal("real server still present after DELETE")
	}
	// Re-register (the VM came back): weight 2 ahead of the feed.
	res = pusher.Push(context.Background(), []hostapi.DirectorEndpoint{epA}, http.MethodPut, "203.0.113.17", "rep_2",
		hostapi.VIPMember{VMIP: "10.200.8.3", Weight: 2, Reason: "serving"})
	if res[0].Err != nil || beA.Table()["tcp/203.0.113.17:443"]["10.200.8.3:443"] != 2 {
		t.Fatalf("register: %v %v", res[0].Err, beA.Table())
	}
}

func TestPushRefusesStrangersAndWrongDirectors(t *testing.T) {
	ca, _ := pki.NewCA("fleet")
	other, _ := pki.NewCA("someone else")
	prog := NewProgrammer(ipvs.NewFake(), t.Logf)
	prog.SetDesired(desired(1))
	_, _ = prog.Apply()
	ep := startDirector(t, ca, "dir_a", prog, nil)
	pool := x509.NewCertPool()
	pool.AddCert(ca.Cert)
	// A client certificate from another CA: the TLS handshake fails.
	strangerCert, _ := leaf(t, other, "hst_x")
	stranger := &Pusher{Cert: func() *tls.Certificate { return &strangerCert }, FleetCA: pool, Timeout: 2 * time.Second}
	res := stranger.Push(context.Background(), []hostapi.DirectorEndpoint{ep}, http.MethodPut, "203.0.113.17", "rep_1",
		hostapi.VIPMember{VMIP: "10.200.4.7"})
	if res[0].Err == nil {
		t.Fatal("a stranger's push was accepted")
	}
	// A director presenting the wrong id: the host refuses to talk to it.
	hostCert, _ := leaf(t, ca, "hst_1")
	pusher := &Pusher{Cert: func() *tls.Certificate { return &hostCert }, FleetCA: pool, Timeout: 2 * time.Second}
	wrong := ep
	wrong.ID = "dir_z"
	res = pusher.Push(context.Background(), []hostapi.DirectorEndpoint{wrong}, http.MethodPut, "203.0.113.17", "rep_1",
		hostapi.VIPMember{VMIP: "10.200.4.7"})
	if res[0].Err == nil || !strings.Contains(res[0].Err.Error(), "expected \"dir_z\"") {
		t.Fatalf("wrong director id accepted: %v", res[0].Err)
	}
	refused := func(name string, c tls.Certificate, vmIP string) {
		t.Helper()
		before := prog.be.(*ipvs.Fake).Table()["tcp/203.0.113.17:443"][vmIP+":443"]
		p := &Pusher{Cert: func() *tls.Certificate { return &c }, FleetCA: pool, Timeout: 2 * time.Second}
		for _, m := range []string{http.MethodPut, http.MethodDelete} {
			r := p.Push(context.Background(), []hostapi.DirectorEndpoint{ep}, m, "203.0.113.17", "rep_x",
				hostapi.VIPMember{VMIP: vmIP, Weight: 5})
			if r[0].Err == nil {
				t.Fatalf("%s: %s accepted", name, m)
			}
		}
		if after := prog.be.(*ipvs.Fake).Table()["tcp/203.0.113.17:443"][vmIP+":443"]; after != before {
			t.Fatalf("%s changed IPVS: %d → %d", name, before, after)
		}
	}
	// Host A (hst_1) on host B's replica (10.200.8.3 is hst_2's).
	refused("host A on B's replica", hostCert, "10.200.8.3")
	// An unknown vm_ip (never registrable: a push cannot add a real server).
	refused("unknown vm_ip", hostCert, "10.200.4.99")
	// A director's certificate (client + server auth), even with a host's CN.
	dirCert, _ := leaf(t, ca, "hst_1", dirEKU...)
	refused("director certificate", dirCert, "10.200.4.7")
	// A certificate chained under a host's SVID issuer (a VM identity).
	ikey, _ := pki.GenerateKey()
	itmpl := &x509.Certificate{SerialNumber: big.NewInt(time.Now().UnixNano()), Subject: pkix.Name{CommonName: "hst_1 SVID issuer"},
		NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour), IsCA: true, BasicConstraintsValid: true,
		KeyUsage: x509.KeyUsageCertSign}
	ider, _ := x509.CreateCertificate(rand.Reader, itmpl, ca.Cert, &ikey.PublicKey, ca.Key)
	icert, _ := x509.ParseCertificate(ider)
	svid, _ := leaf(t, &pki.CA{Cert: icert, Key: ikey}, "hst_1")
	svid.Certificate = append(svid.Certificate, ider)
	refused("SVID-chained certificate", svid, "10.200.4.7")
	// A host the desired state does not know (revoked or never enrolled).
	ghost, _ := leaf(t, ca, "hst_gone")
	refused("unknown host", ghost, "10.200.4.7")
	// Its own member: accepted.
	res = pusher.Push(context.Background(), []hostapi.DirectorEndpoint{ep}, http.MethodPut, "203.0.113.17", "rep_1",
		hostapi.VIPMember{VMIP: "10.200.4.7", Weight: 0})
	if res[0].Err != nil {
		t.Fatalf("own member refused: %v", res[0].Err)
	}
	// A bad vm_ip is a 4xx, never a kernel call.
	res = pusher.Push(context.Background(), []hostapi.DirectorEndpoint{ep}, http.MethodPut, "203.0.113.17", "rep_1",
		hostapi.VIPMember{VMIP: "not-an-ip"})
	if res[0].Err == nil {
		t.Fatal("bad vm_ip accepted")
	}
}
