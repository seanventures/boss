package pki

import (
	"crypto/ecdsa"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"errors"
	"fmt"
	"net/url"
	"sync"
	"time"
)

// SVIDLifetime is the per-VM workload identity lifetime (LLD §3.12.1).
const SVIDLifetime = time.Hour

// SVIDClaims are carried in the SVID subject (OrganizationalUnit entries,
// "key:value"), so any X.509 verifier can read them without a custom OID.
type SVIDClaims struct {
	DeploymentID      string
	DeploymentVersion string
	ImageDigest       string
}

// SVID is one VM's in-memory workload identity. Its key is never written to
// disk and never handed to the guest.
type SVID struct {
	ID    string // spiffe://<trust>/host/<host>/vm/<vm>
	Cert  *x509.Certificate
	Key   *ecdsa.PrivateKey
	chain [][]byte
}

// TLSCertificate returns the SVID with its intermediate, for mTLS upstream.
func (s *SVID) TLSCertificate() tls.Certificate {
	return tls.Certificate{Certificate: s.chain, PrivateKey: s.Key, Leaf: s.Cert}
}

// SpiffeID formats the per-VM identity.
func SpiffeID(trustDomain, hostID, vmID string) string {
	return fmt.Sprintf("spiffe://%s/host/%s/vm/%s", trustDomain, hostID, vmID)
}

// Minter issues and caches per-VM SVIDs from the host intermediate, renewing
// each at two thirds of its lifetime.
type Minter struct {
	mu     sync.Mutex
	creds  func() *HostCreds // current creds (the intermediate rotates on renewal)
	now    func() time.Time
	issued map[string]*SVID
}

func NewMinter(creds func() *HostCreds) *Minter {
	return &Minter{creds: creds, now: time.Now, issued: map[string]*SVID{}}
}

// For returns the VM's current SVID, minting or renewing it as needed.
func (m *Minter) For(vmID string, claims SVIDClaims) (*SVID, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if s, ok := m.issued[vmID]; ok && m.now().Before(RenewAt(s.Cert)) {
		return s, nil
	}
	s, err := m.mint(vmID, claims)
	if err != nil {
		return nil, err
	}
	m.issued[vmID] = s
	return s, nil
}

// Forget drops a stopped VM's SVID from memory.
func (m *Minter) Forget(vmID string) {
	m.mu.Lock()
	delete(m.issued, vmID)
	m.mu.Unlock()
}

func (m *Minter) mint(vmID string, claims SVIDClaims) (*SVID, error) {
	c := m.creds()
	if c == nil || c.SVIDCAKey == nil || c.SVIDCACert == nil {
		return nil, errors.New("svid: host is not enrolled (no intermediate)")
	}
	key, err := GenerateKey()
	if err != nil {
		return nil, err
	}
	id := SpiffeID(c.TrustDomain, c.HostID, vmID)
	u, err := url.Parse(id)
	if err != nil {
		return nil, err
	}
	now := m.now()
	notAfter := now.Add(SVIDLifetime)
	if notAfter.After(c.SVIDCACert.NotAfter) {
		notAfter = c.SVIDCACert.NotAfter // never outlive the intermediate
	}
	var ou []string
	if claims.DeploymentID != "" {
		ou = append(ou, "deployment:"+claims.DeploymentID)
	}
	if claims.DeploymentVersion != "" {
		ou = append(ou, "version:"+claims.DeploymentVersion)
	}
	if claims.ImageDigest != "" {
		ou = append(ou, "image:"+claims.ImageDigest)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          serial(),
		Subject:               pkix.Name{CommonName: vmID, OrganizationalUnit: ou},
		URIs:                  []*url.URL{u},
		NotBefore:             now.Add(-30 * time.Second),
		NotAfter:              notAfter,
		KeyUsage:              x509.KeyUsageDigitalSignature,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
		BasicConstraintsValid: true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, c.SVIDCACert, &key.PublicKey, c.SVIDCAKey)
	if err != nil {
		return nil, err
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		return nil, err
	}
	return &SVID{ID: id, Cert: cert, Key: key, chain: [][]byte{der, c.SVIDCACert.Raw}}, nil
}
