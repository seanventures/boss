package pki

import (
	"crypto/x509"
	"strings"
	"testing"
	"time"
)

func TestSVIDIsSpiffeScopedAndShortLived(t *testing.T) {
	ca, _ := NewCA("root")
	sk, _ := GenerateKey()
	csrPEM, _ := CSR(sk, "svid-ca")
	csr, err := ParseCSR(csrPEM)
	if err != nil {
		t.Fatal(err)
	}
	der, err := ca.SignSVIDCA(csr, "inst.boss", "hst_9", 24*time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	inter, _ := x509.ParseCertificate(der)
	creds := &HostCreds{HostID: "hst_9", TrustDomain: "inst.boss", SVIDCAKey: sk, SVIDCACert: inter}
	m := NewMinter(func() *HostCreds { return creds })
	s, err := m.For("vm-3", SVIDClaims{DeploymentID: "dep", ImageDigest: "sha256:abc"})
	if err != nil {
		t.Fatal(err)
	}
	if s.ID != "spiffe://inst.boss/host/hst_9/vm/vm-3" || s.Cert.URIs[0].String() != s.ID {
		t.Fatalf("id %s", s.ID)
	}
	if life := s.Cert.NotAfter.Sub(s.Cert.NotBefore); life > SVIDLifetime+time.Minute {
		t.Fatalf("SVID lifetime %s", life)
	}
	if !strings.Contains(strings.Join(s.Cert.Subject.OrganizationalUnit, ","), "image:sha256:abc") {
		t.Fatal("claims missing")
	}
	roots := x509.NewCertPool()
	roots.AddCert(ca.Cert)
	inters := x509.NewCertPool()
	inters.AddCert(inter)
	if _, err := s.Cert.Verify(x509.VerifyOptions{Roots: roots, Intermediates: inters, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}}); err != nil {
		t.Fatalf("SVID does not chain to the root: %v", err)
	}
	again, _ := m.For("vm-3", SVIDClaims{})
	if again != s {
		t.Fatal("SVID must be cached until 2/3 of its life")
	}
	// The intermediate is name-constrained: another trust domain fails.
	evil := &HostCreds{HostID: "hst_9", TrustDomain: "other.boss", SVIDCAKey: sk, SVIDCACert: inter}
	bad, _ := NewMinter(func() *HostCreds { return evil }).For("vm-1", SVIDClaims{})
	if _, err := bad.Cert.Verify(x509.VerifyOptions{Roots: roots, Intermediates: inters, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageAny}}); err == nil {
		t.Fatal("an SVID outside the trust domain must not verify")
	}
}

func TestRenewAtTwoThirds(t *testing.T) {
	now := time.Now()
	c := &x509.Certificate{NotBefore: now, NotAfter: now.Add(24 * time.Hour)}
	if got := RenewAt(c).Sub(now); got != 16*time.Hour {
		t.Fatal(got)
	}
}

func TestCredsRoundTrip(t *testing.T) {
	ca, _ := NewCA("root")
	ck, _ := GenerateKey()
	sk, _ := GenerateKey()
	cp, _ := CSR(ck, "c")
	csr, _ := ParseCSR(cp)
	der, _ := ca.SignClient(csr, "hst_1", time.Hour)
	cert, _ := x509.ParseCertificate(der)
	c := &HostCreds{HostID: "hst_1", TrustDomain: "t.boss", VMPrefix: "10.200.4.0/23", ClientKey: ck, ClientCert: cert,
		ClientPEM: CertPEM(der), SVIDCAKey: sk, SVIDCACert: cert, SVIDCAPEM: CertPEM(der)}
	dir := t.TempDir()
	if err := c.Save(dir); err != nil {
		t.Fatal(err)
	}
	got, err := LoadHostCreds(dir)
	if err != nil || got.HostID != "hst_1" || got.VMPrefix != "10.200.4.0/23" || !got.ClientKey.Equal(ck) {
		t.Fatalf("%+v %v", got, err)
	}
}
