package gateway

import (
	"context"
	"errors"
	"sync"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
)

// RunKeys is the host's in-memory store of per-run DPoP keys and the
// delegated, per-destination tokens minted for them (P8b S13; LLD §3.12.2;
// HOST_API.md §4.8). Nothing here is ever written to disk, logged, or given to
// a VM: the guest holds run handles only; the host signs.
//
// Rotation: a run's key and tokens are replaced at two thirds of the TTL by
// generating a NEW key and minting fresh tokens for it. The previous pair is
// not revoked by anyone (the control plane revokes at run end), so a forward
// that already captured the old (token, key) pair completes; a forward that
// starts after the swap uses the new one.
type RunKeys struct {
	// Mint asks the control plane for the run's tokens for jkt (cpclient.RunTokens).
	Mint func(ctx context.Context, a hostapi.Assignment, req hostapi.RunTokensRequest) (*hostapi.RunTokensResponse, error)
	// Audiences requested per run (default hostapi.HostAudiences).
	Audiences []string
	// Now is the clock (tests).
	Now func() time.Time
	// Log reports mint failures (optional).
	Log func(format string, args ...any)

	mu   sync.Mutex
	runs map[string]*runKeys
}

type runKeys struct {
	assignment hostapi.Assignment
	key        *DPoPKey
	tokens     map[string]hostapi.IssuedToken
	issuedAt   time.Time
	rotateAt   time.Time
	expiresAt  time.Time
	minting    bool
}

// TokenPair is what ONE forward carries: the token for its destination and
// the key that signs its proof. Captured once per request, so a rotation
// mid-flight never pairs a new key with an old token.
type TokenPair struct {
	Token string
	Key   *DPoPKey
	JKT   string
	Exp   time.Time
}

// ErrNoMinter is returned when the store has no control-plane minter.
var ErrNoMinter = errors.New("run keys: no minter")

func (r *RunKeys) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

func (r *RunKeys) audiences() []string {
	if len(r.Audiences) > 0 {
		return r.Audiences
	}
	return hostapi.HostAudiences
}

// Ensure generates this run's key and mints its tokens (first call), or
// rotates them (later calls). It is safe to call concurrently; a second call
// while one is minting returns without a second mint.
func (r *RunKeys) Ensure(ctx context.Context, a hostapi.Assignment) error {
	if r.Mint == nil {
		return ErrNoMinter
	}
	r.mu.Lock()
	if r.runs == nil {
		r.runs = map[string]*runKeys{}
	}
	cur := r.runs[a.RunID]
	if cur == nil {
		cur = &runKeys{assignment: a}
		r.runs[a.RunID] = cur
	}
	if cur.minting {
		r.mu.Unlock()
		return nil
	}
	cur.minting = true
	cur.assignment = a
	r.mu.Unlock()

	key, err := NewDPoPKey()
	if err != nil {
		r.clearMinting(a.RunID)
		return err
	}
	resp, err := r.Mint(ctx, a, hostapi.RunTokensRequest{JKT: key.JKT(), Audiences: r.audiences()})
	if err != nil {
		r.clearMinting(a.RunID)
		if r.Log != nil {
			r.Log("run %s: tokens not minted: %v", a.RunID, err)
		}
		return err
	}
	now := r.now()
	ttl := resp.ExpiresIn
	if ttl <= 0 {
		ttl = 300
	}
	rotate := resp.RotateAfterSeconds
	if rotate <= 0 || rotate >= ttl {
		rotate = (ttl * 2) / 3
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	cur = r.runs[a.RunID]
	if cur == nil { // forgotten while minting: the run ended
		return nil
	}
	cur.minting = false
	cur.key = key
	cur.tokens = map[string]hostapi.IssuedToken{}
	for aud, tok := range resp.Tokens {
		cur.tokens[aud] = tok
	}
	cur.issuedAt = now
	cur.rotateAt = now.Add(time.Duration(rotate) * time.Second)
	cur.expiresAt = now.Add(time.Duration(ttl) * time.Second)
	return nil
}

// clearMinting ends a failed mint. A run that never held a key is dropped
// (nothing usable stays behind); a run being ROTATED keeps its current pair
// until it expires, so in-flight calls are untouched.
func (r *RunKeys) clearMinting(runID string) {
	r.mu.Lock()
	if cur := r.runs[runID]; cur != nil {
		cur.minting = false
		if cur.key == nil {
			delete(r.runs, runID)
		}
	}
	r.mu.Unlock()
}

// Pair returns the run's current (token, key) for one audience. ok is false
// when the run has no tokens yet, none for that audience, or they expired.
func (r *RunKeys) Pair(runID, audience string) (TokenPair, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	cur := r.runs[runID]
	if cur == nil || cur.key == nil {
		return TokenPair{}, false
	}
	tok, ok := cur.tokens[audience]
	if !ok || tok.AccessToken == "" || !r.now().Before(cur.expiresAt) {
		return TokenPair{}, false
	}
	return TokenPair{Token: tok.AccessToken, Key: cur.key, JKT: cur.key.JKT(), Exp: cur.expiresAt}, true
}

// JKT is the run's current key thumbprint ("" when none).
func (r *RunKeys) JKT(runID string) string {
	r.mu.Lock()
	defer r.mu.Unlock()
	if cur := r.runs[runID]; cur != nil && cur.key != nil {
		return cur.key.JKT()
	}
	return ""
}

// Forget drops a run's key and tokens (lease ended, handed back, run gone).
func (r *RunKeys) Forget(runID string) {
	r.mu.Lock()
	delete(r.runs, runID)
	r.mu.Unlock()
}

// Len is how many runs hold keys.
func (r *RunKeys) Len() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.runs)
}

// Due lists the runs whose tokens have reached two thirds of their TTL.
func (r *RunKeys) Due() []hostapi.Assignment {
	r.mu.Lock()
	defer r.mu.Unlock()
	now := r.now()
	var out []hostapi.Assignment
	for _, cur := range r.runs {
		if cur.key != nil && !cur.minting && !now.Before(cur.rotateAt) {
			out = append(out, cur.assignment)
		}
	}
	return out
}

// RotateDue rotates every due run once (the loop's tick, and tests).
func (r *RunKeys) RotateDue(ctx context.Context) {
	for _, a := range r.Due() {
		_ = r.Ensure(ctx, a)
	}
}

// RotateLoop rotates due runs every `every` until ctx ends.
func (r *RunKeys) RotateLoop(ctx context.Context, every time.Duration) {
	if every <= 0 {
		every = 5 * time.Second
	}
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			r.RotateDue(ctx)
		}
	}
}

// Headers returns the two headers one forward carries for pair: the token
// for its destination and a fresh proof over (method, htu).
func (p TokenPair) Headers(method, htu string, now time.Time) (map[string]string, error) {
	proof, err := p.Key.Proof(method, htu, p.Token, now)
	if err != nil {
		return nil, err
	}
	return map[string]string{hostapi.HeaderDelegation: p.Token, hostapi.HeaderDPoP: proof}, nil
}
