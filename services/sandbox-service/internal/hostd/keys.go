package hostd

import (
	"crypto/ecdsa"

	"github.com/seanheiney/boss/services/sandbox-service/internal/pki"
)

type ecdsaKey = ecdsa.PrivateKey

// freshCSRs makes new client and intermediate keys and their CSRs.
func freshCSRs(hostname string) (ck, sk *ecdsa.PrivateKey, csr, caCSR string, err error) {
	if ck, err = pki.GenerateKey(); err != nil {
		return
	}
	if sk, err = pki.GenerateKey(); err != nil {
		return
	}
	if csr, err = pki.CSR(ck, "host-client:"+hostname); err != nil {
		return
	}
	caCSR, err = pki.CSR(sk, "host-svid-ca:"+hostname)
	return
}
