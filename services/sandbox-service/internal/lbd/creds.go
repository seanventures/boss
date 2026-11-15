package lbd

import (
	"crypto/ecdsa"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/seanheiney/boss/services/sandbox-service/internal/pki"
)

// Creds is the director's fleet identity: a 24 h certificate from the
// fleet CA (client and server auth: it both calls the control plane and
// serves host pushes) and the pinned fleet CA. Files 0600 under <state>/pki.
type Creds struct {
	ID          string
	TrustDomain string
	Key         *ecdsa.PrivateKey
	Cert        *x509.Certificate
	CertPEM     string
	FleetCAPEM  string
}

// TLSCertificate is the mTLS certificate (both directions).
func (c *Creds) TLSCertificate() tls.Certificate {
	return tls.Certificate{Certificate: [][]byte{c.Cert.Raw}, PrivateKey: c.Key, Leaf: c.Cert}
}

// FleetPool is the pinned fleet CA as a pool.
func (c *Creds) FleetPool() (*x509.CertPool, error) { return pki.PoolFromPEM([]byte(c.FleetCAPEM)) }

const (
	credMeta  = "director.meta"
	credKey   = "director.key"
	credCert  = "director.crt"
	credFleet = "fleet-ca.pem"
)

// Save writes the files atomically.
func (c *Creds) Save(dir string) error {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	key, err := pki.KeyPEM(c.Key)
	if err != nil {
		return err
	}
	files := map[string][]byte{credMeta: []byte(c.ID + "\n" + c.TrustDomain + "\n"), credKey: key,
		credCert: []byte(c.CertPEM), credFleet: []byte(c.FleetCAPEM)}
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

// LoadCreds reads what Save wrote; os.ErrNotExist = not enrolled.
func LoadCreds(dir string) (*Creds, error) {
	meta, err := os.ReadFile(filepath.Join(dir, credMeta))
	if err != nil {
		return nil, err
	}
	parts := strings.SplitN(string(meta), "\n", 3)
	if len(parts) < 2 || parts[0] == "" {
		return nil, errors.New("director.meta is malformed")
	}
	c := &Creds{ID: parts[0], TrustDomain: parts[1]}
	keyRaw, err := os.ReadFile(filepath.Join(dir, credKey))
	if err != nil {
		return nil, err
	}
	certRaw, err := os.ReadFile(filepath.Join(dir, credCert))
	if err != nil {
		return nil, err
	}
	fleet, err := os.ReadFile(filepath.Join(dir, credFleet))
	if err != nil {
		return nil, err
	}
	if c.Key, err = pki.ParseKey(keyRaw); err != nil {
		return nil, fmt.Errorf("director key: %w", err)
	}
	c.CertPEM = string(certRaw)
	if c.Cert, err = pki.ParseCert(c.CertPEM); err != nil {
		return nil, fmt.Errorf("director cert: %w", err)
	}
	c.FleetCAPEM = string(fleet)
	return c, nil
}

// LoadFiles builds Creds from explicit PEM files (lab mode: no enrolment).
func LoadFiles(id, keyPath, certPath, caPath string) (*Creds, error) {
	keyRaw, err := os.ReadFile(keyPath)
	if err != nil {
		return nil, err
	}
	certRaw, err := os.ReadFile(certPath)
	if err != nil {
		return nil, err
	}
	caRaw, err := os.ReadFile(caPath)
	if err != nil {
		return nil, err
	}
	c := &Creds{ID: id, CertPEM: string(certRaw), FleetCAPEM: string(caRaw)}
	if c.Key, err = pki.ParseKey(keyRaw); err != nil {
		return nil, fmt.Errorf("key %s: %w", keyPath, err)
	}
	if c.Cert, err = pki.ParseCert(c.CertPEM); err != nil {
		return nil, fmt.Errorf("cert %s: %w", certPath, err)
	}
	if id == "" {
		c.ID = c.Cert.Subject.CommonName
	}
	return c, nil
}
