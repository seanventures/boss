package gateway

import (
	"crypto/ecdsa"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"time"
)

// RunAssertion is the short signed statement boss-hostd attaches to every
// forwarded call (LLD §3.12.1, Transaction-Token shaped). It is signed with
// the VM's SVID key, so the receiver checks it against the mTLS peer chain.
type RunAssertion struct {
	Iss        string `json:"iss"` // the VM's SPIFFE ID
	Aud        string `json:"aud"` // route
	Run        string `json:"run,omitempty"`
	Epoch      int64  `json:"epoch,omitempty"`
	VM         string `json:"vm"`
	Deployment string `json:"deployment,omitempty"`
	Iat        int64  `json:"iat"`
	Exp        int64  `json:"exp"`
	Jti        string `json:"jti"`
}

// AssertionHeader is the forwarded header name.
const AssertionHeader = "Boss-Run-Assertion"

var b64 = base64.RawURLEncoding

// SignAssertion makes a compact ES256 JWS.
func SignAssertion(key *ecdsa.PrivateKey, a RunAssertion) (string, error) {
	hdr, _ := json.Marshal(map[string]string{"alg": "ES256", "typ": "boss-run+jwt"})
	body, err := json.Marshal(a)
	if err != nil {
		return "", err
	}
	signing := b64.EncodeToString(hdr) + "." + b64.EncodeToString(body)
	sum := sha256.Sum256([]byte(signing))
	r, s, err := ecdsa.Sign(rand.Reader, key, sum[:])
	if err != nil {
		return "", err
	}
	sig := make([]byte, 64)
	r.FillBytes(sig[:32])
	s.FillBytes(sig[32:])
	return signing + "." + b64.EncodeToString(sig), nil
}

func newAssertion(iss, aud string, b Binding, now time.Time) RunAssertion {
	jti := make([]byte, 12)
	_, _ = rand.Read(jti)
	return RunAssertion{Iss: iss, Aud: aud, Run: b.RunID, Epoch: b.Epoch, VM: b.VMID,
		Deployment: b.DeploymentID, Iat: now.Unix(), Exp: now.Add(60 * time.Second).Unix(),
		Jti: b64.EncodeToString(jti)}
}
