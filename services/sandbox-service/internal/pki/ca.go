package pki

import (
	"crypto/ecdsa"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"net"
	"time"
)

// CA is a minimal BOSS-style certificate authority. The real one lives in the
// Python control plane; this one backs the Go fake control plane and tests,
// and documents exactly which certificate shapes boss-hostd expects.
type CA struct {
	Cert *x509.Certificate
	Key  *ecdsa.PrivateKey
	PEM  string
}

// NewCA makes a self-signed root.
func NewCA(name string) (*CA, error) {
	key, err := GenerateKey()
	if err != nil {
		return nil, err
	}
	now := time.Now()
	tmpl := &x509.Certificate{
		SerialNumber:          serial(),
		Subject:               pkix.Name{CommonName: name},
		NotBefore:             now.Add(-time.Minute),
		NotAfter:              now.Add(10 * 365 * 24 * time.Hour),
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		return nil, err
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		return nil, err
	}
	return &CA{Cert: cert, Key: key, PEM: CertPEM(der)}, nil
}

// ServerCert issues a TLS server certificate for the given DNS names / IPs.
func (ca *CA) ServerCert(names ...string) (tls.Certificate, error) {
	key, err := GenerateKey()
	if err != nil {
		return tls.Certificate{}, err
	}
	now := time.Now()
	tmpl := &x509.Certificate{
		SerialNumber: serial(),
		Subject:      pkix.Name{CommonName: names[0]},
		NotBefore:    now.Add(-time.Minute),
		NotAfter:     now.Add(24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	for _, n := range names {
		if ip := net.ParseIP(n); ip != nil {
			tmpl.IPAddresses = append(tmpl.IPAddresses, ip)
		} else {
			tmpl.DNSNames = append(tmpl.DNSNames, n)
		}
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca.Cert, &key.PublicKey, ca.Key)
	if err != nil {
		return tls.Certificate{}, err
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}, nil
}

// backdate tolerates small clock skew without distorting short test TTLs.
func backdate(ttl time.Duration) time.Duration {
	if d := ttl / 20; d < time.Minute {
		return d
	}
	return time.Minute
}

// SignClient issues the host's mTLS client certificate (CN=host:<id>).
func (ca *CA) SignClient(csr *x509.CertificateRequest, hostID string, ttl time.Duration) ([]byte, error) {
	now := time.Now()
	tmpl := &x509.Certificate{
		SerialNumber: serial(),
		Subject:      pkix.Name{CommonName: "host:" + hostID},
		NotBefore:    now.Add(-backdate(ttl)),
		NotAfter:     now.Add(ttl),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
	}
	return x509.CreateCertificate(rand.Reader, tmpl, ca.Cert, csr.PublicKey, ca.Key)
}

// SignSVIDCA issues the host-scoped SVID intermediate: CA:true, pathlen 0,
// and name-constrained to spiffe URIs in the install's trust domain.
func (ca *CA) SignSVIDCA(csr *x509.CertificateRequest, trustDomain, hostID string, ttl time.Duration) ([]byte, error) {
	now := time.Now()
	tmpl := &x509.Certificate{
		SerialNumber:          serial(),
		Subject:               pkix.Name{CommonName: "svid-ca:host:" + hostID},
		NotBefore:             now.Add(-backdate(ttl)),
		NotAfter:              now.Add(ttl),
		KeyUsage:              x509.KeyUsageCertSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
		MaxPathLenZero:        true,
		PermittedURIDomains:   []string{trustDomain},
	}
	return x509.CreateCertificate(rand.Reader, tmpl, ca.Cert, csr.PublicKey, ca.Key)
}
