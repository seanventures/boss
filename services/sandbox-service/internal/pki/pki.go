// Package pki holds boss-hostd's key material: the host client certificate
// (mTLS to the control plane), the host-scoped SVID intermediate, and the
// per-VM SPIFFE-format SVIDs minted from it (LLD §3.2.2, §3.12.1).
//
// Rules this package enforces:
//   - private keys are generated on the host and never leave it; only CSRs do;
//   - key files are written 0600; SVID keys are never written at all;
//   - nothing here ever logs or formats key or certificate bytes.
package pki

import (
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"os"
	"path/filepath"
	"time"
)

// GenerateKey returns a fresh P-256 key.
func GenerateKey() (*ecdsa.PrivateKey, error) {
	return ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
}

// CSR builds a PEM certificate request for key with the given common name.
func CSR(key crypto.Signer, cn string) (string, error) {
	der, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{
		Subject: pkix.Name{CommonName: cn},
	}, key)
	if err != nil {
		return "", err
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: der})), nil
}

// ParseCSR decodes and signature-checks a PEM CSR.
func ParseCSR(p string) (*x509.CertificateRequest, error) {
	block, _ := pem.Decode([]byte(p))
	if block == nil || block.Type != "CERTIFICATE REQUEST" {
		return nil, errors.New("csr: not a PEM CERTIFICATE REQUEST")
	}
	req, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		return nil, err
	}
	if err := req.CheckSignature(); err != nil {
		return nil, fmt.Errorf("csr: bad signature: %w", err)
	}
	return req, nil
}

// ParseCert decodes the first PEM certificate.
func ParseCert(p string) (*x509.Certificate, error) {
	block, _ := pem.Decode([]byte(p))
	if block == nil || block.Type != "CERTIFICATE" {
		return nil, errors.New("cert: not a PEM CERTIFICATE")
	}
	return x509.ParseCertificate(block.Bytes)
}

// CertPEM encodes DER as PEM.
func CertPEM(der []byte) string {
	return string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}))
}

// KeyPEM encodes an EC key as PEM (for the 0600 key files only).
func KeyPEM(key *ecdsa.PrivateKey) ([]byte, error) {
	der, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: der}), nil
}

// ParseKey decodes an EC PEM private key.
func ParseKey(p []byte) (*ecdsa.PrivateKey, error) {
	block, _ := pem.Decode(p)
	if block == nil {
		return nil, errors.New("key: not PEM")
	}
	return x509.ParseECPrivateKey(block.Bytes)
}

// Fingerprint is the lowercase hex SHA-256 of a certificate's DER. It is a
// public value (it identifies, it does not authenticate).
func Fingerprint(cert *x509.Certificate) string {
	sum := sha256.Sum256(cert.Raw)
	return hex.EncodeToString(sum[:])
}

// RenewAt is the instant a certificate should be renewed: two thirds of the
// way through its validity window (LLD §3.2.2).
func RenewAt(cert *x509.Certificate) time.Time {
	life := cert.NotAfter.Sub(cert.NotBefore)
	return cert.NotBefore.Add(life * 2 / 3)
}

// PoolFromPEM builds a cert pool from PEM bytes; it errors if there is none.
func PoolFromPEM(p []byte) (*x509.CertPool, error) {
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(p) {
		return nil, errors.New("no PEM certificates found")
	}
	return pool, nil
}

func serial() *big.Int {
	n, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 126))
	return n
}

// ---- Host credential store (files under <state>/pki, 0600) ----------------

// HostCreds is what enrolment produces and renewal refreshes.
type HostCreds struct {
	HostID      string
	TrustDomain string
	VMPrefix    string
	ClientKey   *ecdsa.PrivateKey
	ClientCert  *x509.Certificate
	ClientPEM   string
	SVIDCAKey   *ecdsa.PrivateKey
	SVIDCACert  *x509.Certificate
	SVIDCAPEM   string
	// FleetCAPEM is the fleet CA from enrolment, pinned from then on: a
	// renewal answered under a different CA is refused.
	FleetCAPEM string
}

// TLSCertificate returns the mTLS client certificate.
func (c *HostCreds) TLSCertificate() tls.Certificate {
	return tls.Certificate{
		Certificate: [][]byte{c.ClientCert.Raw},
		PrivateKey:  c.ClientKey,
		Leaf:        c.ClientCert,
	}
}

const (
	fileMeta     = "host.meta"
	fileClientK  = "client.key"
	fileClientC  = "client.crt"
	fileSVIDCAK  = "svid-ca.key"
	fileSVIDCAC  = "svid-ca.crt"
	fileFleetCA  = "fleet-ca.pem"
	metaSep      = "\n"
	stateDirMode = 0o700
)

// Save writes the credentials atomically (write tmp + rename), keys 0600.
func (c *HostCreds) Save(dir string) error {
	if err := os.MkdirAll(dir, stateDirMode); err != nil {
		return err
	}
	ck, err := KeyPEM(c.ClientKey)
	if err != nil {
		return err
	}
	sk, err := KeyPEM(c.SVIDCAKey)
	if err != nil {
		return err
	}
	files := map[string][]byte{
		fileMeta:    []byte(c.HostID + metaSep + c.TrustDomain + metaSep + c.VMPrefix + metaSep),
		fileClientK: ck,
		fileClientC: []byte(c.ClientPEM),
		fileSVIDCAK: sk,
		fileSVIDCAC: []byte(c.SVIDCAPEM),
		fileFleetCA: []byte(c.FleetCAPEM),
	}
	for name, data := range files {
		tmp := filepath.Join(dir, "."+name+".tmp")
		if err := os.WriteFile(tmp, data, 0o600); err != nil {
			return err
		}
		if err := os.Rename(tmp, filepath.Join(dir, name)); err != nil {
			return err
		}
	}
	return nil
}

// LoadHostCreds reads credentials written by Save. os.ErrNotExist means the
// host has not enrolled yet.
func LoadHostCreds(dir string) (*HostCreds, error) {
	meta, err := os.ReadFile(filepath.Join(dir, fileMeta))
	if err != nil {
		return nil, err
	}
	var parts [3]string
	cur := 0
	start := 0
	for i := 0; i < len(meta) && cur < 3; i++ {
		if meta[i] == '\n' {
			parts[cur] = string(meta[start:i])
			cur++
			start = i + 1
		}
	}
	if cur < 3 || parts[0] == "" {
		return nil, errors.New("host.meta is malformed")
	}
	read := func(name string) ([]byte, error) { return os.ReadFile(filepath.Join(dir, name)) }
	ckRaw, err := read(fileClientK)
	if err != nil {
		return nil, err
	}
	ccRaw, err := read(fileClientC)
	if err != nil {
		return nil, err
	}
	skRaw, err := read(fileSVIDCAK)
	if err != nil {
		return nil, err
	}
	scRaw, err := read(fileSVIDCAC)
	if err != nil {
		return nil, err
	}
	fleetCA, _ := read(fileFleetCA)
	c := &HostCreds{HostID: parts[0], TrustDomain: parts[1], VMPrefix: parts[2],
		ClientPEM: string(ccRaw), SVIDCAPEM: string(scRaw), FleetCAPEM: string(fleetCA)}
	if c.ClientKey, err = ParseKey(ckRaw); err != nil {
		return nil, fmt.Errorf("client key: %w", err)
	}
	if c.ClientCert, err = ParseCert(c.ClientPEM); err != nil {
		return nil, fmt.Errorf("client cert: %w", err)
	}
	if c.SVIDCAKey, err = ParseKey(skRaw); err != nil {
		return nil, fmt.Errorf("svid ca key: %w", err)
	}
	if c.SVIDCACert, err = ParseCert(c.SVIDCAPEM); err != nil {
		return nil, fmt.Errorf("svid ca cert: %w", err)
	}
	return c, nil
}
