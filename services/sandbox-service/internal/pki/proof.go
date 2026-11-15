package pki

import (
	"crypto/ecdsa"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"math/big"
	"strings"
	"time"
)

// The BossHost proof (HOST_API.md §Auth; engine/boss_engine/services/fleet_ca.py):
//
//	Authorization: BossHost <compact JWS>
//	header {"alg":"ES256","typ":"boss-host+jwt","x5t#S256":b64url(sha256(cert DER))}
//	claims {"htm":"POST","htu":"/v1/hosts/<id>/heartbeat","iat":<unix s>,"jti":<16..128 chars>}
//
// It proves possession of the host certificate's key on every call, through
// any TLS terminator. The control plane pins the certificate by fingerprint,
// checks htm/htu/iat and spends the jti once.

const proofTyp = "boss-host+jwt"

var rawB64 = base64.RawURLEncoding

type proofHeader struct {
	Alg string `json:"alg"`
	Typ string `json:"typ"`
	X5t string `json:"x5t#S256"`
}

// ProofClaims are the signed claims.
type ProofClaims struct {
	Htm string `json:"htm"`
	Htu string `json:"htu"`
	Iat int64  `json:"iat"`
	Jti string `json:"jti"`
}

// SignHostProof signs a proof for one request (path without the query).
func SignHostProof(key *ecdsa.PrivateKey, cert *x509.Certificate, method, path string) (string, error) {
	if key == nil || cert == nil {
		return "", errors.New("host proof: no key or certificate")
	}
	fp := sha256.Sum256(cert.Raw)
	h, _ := json.Marshal(proofHeader{Alg: "ES256", Typ: proofTyp, X5t: rawB64.EncodeToString(fp[:])})
	jti := make([]byte, 18)
	if _, err := rand.Read(jti); err != nil {
		return "", err
	}
	c, _ := json.Marshal(ProofClaims{Htm: strings.ToUpper(method), Htu: path, Iat: time.Now().Unix(),
		Jti: rawB64.EncodeToString(jti)})
	signing := rawB64.EncodeToString(h) + "." + rawB64.EncodeToString(c)
	sum := sha256.Sum256([]byte(signing))
	r, s, err := ecdsa.Sign(rand.Reader, key, sum[:])
	if err != nil {
		return "", err
	}
	sig := make([]byte, 64)
	r.FillBytes(sig[:32])
	s.FillBytes(sig[32:])
	return signing + "." + rawB64.EncodeToString(sig), nil
}

// ParseHostProof splits a proof and returns the certificate fingerprint it
// names (hex) and its claims, without verifying the signature.
func ParseHostProof(token string) (fingerprint string, claims ProofClaims, err error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return "", claims, errors.New("proof_malformed")
	}
	hb, err1 := rawB64.DecodeString(parts[0])
	cb, err2 := rawB64.DecodeString(parts[1])
	if err1 != nil || err2 != nil {
		return "", claims, errors.New("proof_malformed")
	}
	var h proofHeader
	if json.Unmarshal(hb, &h) != nil || json.Unmarshal(cb, &claims) != nil {
		return "", claims, errors.New("proof_malformed")
	}
	if h.Typ != proofTyp || h.Alg != "ES256" {
		return "", claims, errors.New("proof_bad_typ")
	}
	fpRaw, err := rawB64.DecodeString(h.X5t)
	if err != nil || len(fpRaw) != 32 {
		return "", claims, errors.New("proof_no_certificate")
	}
	return hexString(fpRaw), claims, nil
}

// VerifyHostProof checks the signature against the pinned certificate and the
// method, path and age. Replay (jti) is the caller's job.
func VerifyHostProof(token string, cert *x509.Certificate, method, path string, now time.Time) (ProofClaims, error) {
	_, claims, err := ParseHostProof(token)
	if err != nil {
		return claims, err
	}
	if !strings.EqualFold(claims.Htm, method) {
		return claims, errors.New("proof_htm")
	}
	if claims.Htu != path {
		return claims, errors.New("proof_htu")
	}
	if age := now.Unix() - claims.Iat; age > 90 || age < -30 {
		return claims, errors.New("proof_iat")
	}
	if len(claims.Jti) < 16 || len(claims.Jti) > 128 {
		return claims, errors.New("proof_jti")
	}
	parts := strings.Split(token, ".")
	sig, err := rawB64.DecodeString(parts[2])
	if err != nil || len(sig) != 64 {
		return claims, errors.New("proof_bad_alg")
	}
	pub, ok := cert.PublicKey.(*ecdsa.PublicKey)
	if !ok {
		return claims, errors.New("proof_bad_alg")
	}
	sum := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
	if !ecdsa.Verify(pub, sum[:], new(big.Int).SetBytes(sig[:32]), new(big.Int).SetBytes(sig[32:])) {
		return claims, errors.New("proof_signature_invalid")
	}
	return claims, nil
}

func hexString(b []byte) string {
	const digits = "0123456789abcdef"
	out := make([]byte, len(b)*2)
	for i, c := range b {
		out[2*i], out[2*i+1] = digits[c>>4], digits[c&15]
	}
	return string(out)
}
