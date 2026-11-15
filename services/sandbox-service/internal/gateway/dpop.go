package gateway

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"time"
)

// DPoP (RFC 9449) proofs signed by a HOST-held Ed25519 key (P8b S13; LLD
// §3.12.2). The control plane mints a run's tokens for the key's thumbprint
// (`cnf.jkt`); every forward to a central gateway carries one token for THAT
// destination plus a fresh proof over the forwarded method and URL. The
// format is exactly what engine/boss_engine/identity/dpop.verify_proof reads:
//
//	header {"alg":"EdDSA","typ":"dpop+jwt","jwk":{"kty":"OKP","crv":"Ed25519","x":…}}
//	claims {"jti","htm","htu","iat","ath"}
//
// htu is normalised as the verifier does (scheme://host[:port]/path, no
// query); ath is b64url(sha256(access_token)).

const dpopTyp = "dpop+jwt"

var rawURL = base64.RawURLEncoding

// DPoPKey is one Ed25519 key pair held in memory. It is never serialised.
type DPoPKey struct {
	priv ed25519.PrivateKey
	pub  ed25519.PublicKey
	jkt  string
}

// NewDPoPKey generates a key pair and its RFC 7638 thumbprint.
func NewDPoPKey() (*DPoPKey, error) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, err
	}
	return &DPoPKey{priv: priv, pub: pub, jkt: Thumbprint(pub)}, nil
}

// JKT is the key's RFC 7638 thumbprint (the `cnf.jkt` the tokens carry).
func (k *DPoPKey) JKT() string { return k.jkt }

// PublicJWK is the public half as a JWK.
func (k *DPoPKey) PublicJWK() map[string]string {
	return map[string]string{"kty": "OKP", "crv": "Ed25519", "x": rawURL.EncodeToString(k.pub)}
}

// Thumbprint is the RFC 7638 thumbprint of an Ed25519 public key: SHA-256 of
// the required members in lexicographic order, compact JSON, base64url —
// byte for byte what engine/boss_engine/identity/jwks.thumbprint computes.
func Thumbprint(pub ed25519.PublicKey) string {
	canon := `{"crv":"Ed25519","kty":"OKP","x":"` + rawURL.EncodeToString(pub) + `"}`
	sum := sha256.Sum256([]byte(canon))
	return rawURL.EncodeToString(sum[:])
}

// NormalizeHTU is RFC 9449 §4.3: scheme and host lower-cased, default ports
// dropped, no query or fragment.
func NormalizeHTU(scheme, host, path string) string {
	scheme = strings.ToLower(scheme)
	host = strings.ToLower(host)
	if (scheme == "https" && strings.HasSuffix(host, ":443")) || (scheme == "http" && strings.HasSuffix(host, ":80")) {
		host = host[:strings.LastIndexByte(host, ':')]
	}
	if path == "" {
		path = "/"
	}
	if i := strings.IndexAny(path, "?#"); i >= 0 {
		path = path[:i]
	}
	return scheme + "://" + host + path
}

type dpopClaims struct {
	JTI string `json:"jti"`
	HTM string `json:"htm"`
	HTU string `json:"htu"`
	IAT int64  `json:"iat"`
	ATH string `json:"ath,omitempty"`
}

// Proof signs a DPoP proof for one request: method, the normalised URL and
// the access token it accompanies.
func (k *DPoPKey) Proof(method, htu, accessToken string, now time.Time) (string, error) {
	if k == nil || k.priv == nil {
		return "", errors.New("dpop: no key")
	}
	hdr, err := json.Marshal(map[string]any{"alg": "EdDSA", "typ": dpopTyp, "jwk": k.PublicJWK()})
	if err != nil {
		return "", err
	}
	jti := make([]byte, 16)
	if _, err := rand.Read(jti); err != nil {
		return "", err
	}
	c := dpopClaims{JTI: rawURL.EncodeToString(jti), HTM: strings.ToUpper(method), HTU: htu, IAT: now.Unix()}
	if accessToken != "" {
		sum := sha256.Sum256([]byte(accessToken))
		c.ATH = rawURL.EncodeToString(sum[:])
	}
	body, err := json.Marshal(c)
	if err != nil {
		return "", err
	}
	signing := rawURL.EncodeToString(hdr) + "." + rawURL.EncodeToString(body)
	sig := ed25519.Sign(k.priv, []byte(signing))
	return signing + "." + rawURL.EncodeToString(sig), nil
}

// VerifyProof checks a proof the way the control plane does (tests and the
// local lane): header, signature by the embedded key, thumbprint, htm, htu,
// age and ath. It returns the proof key's thumbprint.
func VerifyProof(proof, method, htu, accessToken string, now time.Time) (string, error) {
	parts := strings.Split(proof, ".")
	if len(parts) != 3 {
		return "", errors.New("dpop_malformed")
	}
	hb, err1 := rawURL.DecodeString(parts[0])
	cb, err2 := rawURL.DecodeString(parts[1])
	sig, err3 := rawURL.DecodeString(parts[2])
	if err1 != nil || err2 != nil || err3 != nil {
		return "", errors.New("dpop_malformed")
	}
	var hdr struct {
		Alg  string            `json:"alg"`
		Typ  string            `json:"typ"`
		JWK  map[string]string `json:"jwk"`
		Crit []string          `json:"crit"`
	}
	if json.Unmarshal(hb, &hdr) != nil || hdr.Alg != "EdDSA" || hdr.Typ != dpopTyp || hdr.Crit != nil {
		return "", errors.New("dpop_bad_header")
	}
	if hdr.JWK["kty"] != "OKP" || hdr.JWK["crv"] != "Ed25519" {
		return "", errors.New("dpop_bad_jwk")
	}
	if _, private := hdr.JWK["d"]; private {
		return "", errors.New("dpop_bad_jwk")
	}
	x, err := rawURL.DecodeString(hdr.JWK["x"])
	if err != nil || len(x) != ed25519.PublicKeySize {
		return "", errors.New("dpop_bad_jwk")
	}
	pub := ed25519.PublicKey(x)
	if !ed25519.Verify(pub, []byte(parts[0]+"."+parts[1]), sig) {
		return "", errors.New("dpop_bad_signature")
	}
	var c dpopClaims
	if json.Unmarshal(cb, &c) != nil {
		return "", errors.New("dpop_malformed")
	}
	if !strings.EqualFold(c.HTM, method) {
		return "", errors.New("dpop_htm")
	}
	if c.HTU != htu {
		return "", errors.New("dpop_htu")
	}
	if age := now.Unix() - c.IAT; age > 90 || age < -30 {
		return "", errors.New("dpop_iat")
	}
	if accessToken != "" {
		sum := sha256.Sum256([]byte(accessToken))
		if c.ATH != rawURL.EncodeToString(sum[:]) {
			return "", errors.New("dpop_ath")
		}
	}
	if len(c.JTI) < 8 || len(c.JTI) > 128 {
		return "", errors.New("dpop_jti")
	}
	return Thumbprint(pub), nil
}
