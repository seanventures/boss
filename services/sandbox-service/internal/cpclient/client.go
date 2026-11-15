// Package cpclient is boss-hostd's client for the control-plane host API
// (docs/infra/HOST_API.md).
//
// Transport: https verified against the pinned CA bundle only (never the
// system roots); plain http is allowed to a loopback control plane only (dev
// and CI). Auth: every call after enrolment carries a fresh BossHost proof
// signed by the host certificate's key, and the TLS client certificate is
// presented whenever the server (or a TLS terminator) asks for it.
package cpclient

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
)

// ErrNotSupported is returned when the control plane has no such endpoint.
var ErrNotSupported = errors.New("control plane does not serve this endpoint")

// APIError is a non-2xx answer with the control plane's named code.
type APIError struct {
	Status  int
	Reason  string
	Message string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("control plane %d %s: %s", e.Status, e.Reason, e.Message)
}

// IsRevoked reports whether err is the control plane refusing a revoked host.
func IsRevoked(err error) bool {
	var ae *APIError
	return errors.As(err, &ae) && ae.Reason == hostapi.ReasonHostRevoked
}

// Prover signs a BossHost proof for one request.
type Prover func(method, path string) (string, error)

// Client talks to one control-plane base URL.
type Client struct {
	BaseURL string
	// ClientCert returns the current TLS client certificate (nil before enrolment).
	ClientCert func() *tls.Certificate
	// Prove signs the BossHost proof (nil before enrolment).
	Prove Prover

	pinned *x509.CertPool
	hc     atomic.Pointer[http.Client]
}

// LoopbackHTTP reports whether u is plain http to a loopback address.
func LoopbackHTTP(u *url.URL) bool {
	if u.Scheme != "http" {
		return false
	}
	ip := net.ParseIP(u.Hostname())
	return u.Hostname() == "localhost" || (ip != nil && ip.IsLoopback())
}

// New builds a client that trusts only the pinned CA bundle.
func New(baseURL string, pinnedCA *x509.CertPool, clientCert func() *tls.Certificate) (*Client, error) {
	u, err := url.Parse(strings.TrimRight(baseURL, "/"))
	if err != nil || u.Host == "" {
		return nil, fmt.Errorf("control plane URL %q is not a URL", baseURL)
	}
	if u.Scheme != "https" && !LoopbackHTTP(u) {
		return nil, fmt.Errorf("control plane URL must be https (plain http only to loopback)")
	}
	if u.Scheme == "https" && pinnedCA == nil {
		return nil, fmt.Errorf("an https control plane needs the pinned CA (BOSS_HOSTD_CP_CA)")
	}
	c := &Client{BaseURL: u.String(), ClientCert: clientCert, pinned: pinnedCA}
	c.hc.Store(c.newHTTP())
	return c, nil
}

func (c *Client) newHTTP() *http.Client {
	tlsCfg := &tls.Config{
		RootCAs:    c.pinned,
		MinVersion: tls.VersionTLS12,
		GetClientCertificate: func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
			if c.ClientCert != nil {
				if cert := c.ClientCert(); cert != nil {
					return cert, nil
				}
			}
			return &tls.Certificate{}, nil // no cert yet: enrol only
		},
	}
	return &http.Client{Transport: &http.Transport{
		TLSClientConfig:     tlsCfg,
		ForceAttemptHTTP2:   true,
		MaxIdleConnsPerHost: 4,
		IdleConnTimeout:     90 * time.Second,
	}}
}

// Rotate starts a fresh connection pool so every later request handshakes
// with the current certificate. Call it after enrolment and after each
// renewal: HTTP/2 multiplexes new requests onto old connections otherwise.
// In-flight requests (a long-poll) finish on the old pool.
func (c *Client) Rotate() {
	old := c.hc.Swap(c.newHTTP())
	if t, ok := old.Transport.(*http.Transport); ok {
		t.CloseIdleConnections()
	}
}

// HTTPClient exposes the pinned client (the guest gateway reuses its trust
// for run-plane forwards to the control plane).
func (c *Client) HTTPClient() *http.Client { return c.hc.Load() }

// parseError reads the control plane's error body. FastAPI wraps it as
// {"detail": {"error": code, "message": ...}}; plain {"error","reason"} and a
// string detail are accepted too.
func parseError(status int, raw []byte) *APIError {
	ae := &APIError{Status: status}
	var top map[string]json.RawMessage
	if json.Unmarshal(raw, &top) != nil {
		ae.Message = strings.TrimSpace(string(raw))
		return ae
	}
	read := func(m map[string]json.RawMessage) {
		for _, k := range []string{"code", "reason", "error"} {
			var s string
			if v, ok := m[k]; ok && json.Unmarshal(v, &s) == nil && s != "" && ae.Reason == "" {
				ae.Reason = s
			}
		}
		var msg string
		if v, ok := m["message"]; ok && json.Unmarshal(v, &msg) == nil {
			ae.Message = msg
		}
	}
	if d, ok := top["detail"]; ok {
		var inner map[string]json.RawMessage
		var s string
		if json.Unmarshal(d, &inner) == nil {
			read(inner)
		} else if json.Unmarshal(d, &s) == nil {
			ae.Message = s
		}
	}
	read(top)
	return ae
}

func (c *Client) do(ctx context.Context, method, path string, query url.Values, in, out any, prove bool) (int, error) {
	var body io.Reader
	if in != nil {
		raw, err := json.Marshal(in)
		if err != nil {
			return 0, err
		}
		body = bytes.NewReader(raw)
	}
	full := c.BaseURL + path
	if len(query) > 0 {
		full += "?" + query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, full, body)
	if err != nil {
		return 0, err
	}
	if in != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if prove {
		if c.Prove == nil {
			return 0, errors.New("not enrolled: no host proof")
		}
		// htu is the request path as the server sees it (no query).
		proof, err := c.Prove(method, req.URL.Path)
		if err != nil {
			return 0, err
		}
		req.Header.Set("Authorization", hostapi.AuthScheme+" "+proof)
	}
	req.Header.Set("X-Boss-Host-Api", hostapi.Version)
	resp, err := c.hc.Load().Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return resp.StatusCode, parseError(resp.StatusCode, raw)
	}
	if out != nil && len(raw) > 0 {
		if err := json.Unmarshal(raw, out); err != nil {
			return resp.StatusCode, fmt.Errorf("decode %s: %w", path, err)
		}
	}
	return resp.StatusCode, nil
}

// Enrol spends the one-time join token (in the body; never logged).
func (c *Client) Enrol(ctx context.Context, req hostapi.EnrolRequest) (*hostapi.EnrolResponse, error) {
	var out hostapi.EnrolResponse
	if _, err := c.do(ctx, http.MethodPost, hostapi.PathEnrol, nil, req, &out, false); err != nil {
		return nil, err
	}
	return &out, nil
}

// Renew exchanges fresh CSRs for a new certificate and intermediate.
func (c *Client) Renew(ctx context.Context, hostID string, req hostapi.RenewRequest) (*hostapi.EnrolResponse, error) {
	var out hostapi.EnrolResponse
	if _, err := c.do(ctx, http.MethodPost, fmt.Sprintf(hostapi.PathRenew, hostID), nil, req, &out, true); err != nil {
		return nil, err
	}
	return &out, nil
}

func (c *Client) Heartbeat(ctx context.Context, hostID string, req hostapi.HeartbeatRequest) (*hostapi.HeartbeatResponse, error) {
	var out hostapi.HeartbeatResponse
	if _, err := c.do(ctx, http.MethodPost, fmt.Sprintf(hostapi.PathHeartbeat, hostID), nil, req, &out, true); err != nil {
		return nil, err
	}
	return &out, nil
}

// Observed pushes replica states at once (on a state change).
func (c *Client) Observed(ctx context.Context, hostID string, req hostapi.ObservedRequest) error {
	_, err := c.do(ctx, http.MethodPost, fmt.Sprintf(hostapi.PathObserved, hostID), nil, req, nil, true)
	return err
}

// Desired long-polls for a generation newer than since. It returns
// (nil, nil) when the wait elapsed with no change.
func (c *Client) Desired(ctx context.Context, hostID string, since int64, wait time.Duration) (*hostapi.DesiredState, error) {
	q := url.Values{"since": {strconv.FormatInt(since, 10)}, "wait": {strconv.Itoa(int(wait / time.Second))}}
	var out hostapi.DesiredState
	if _, err := c.do(ctx, http.MethodGet, fmt.Sprintf(hostapi.PathDesired, hostID), q, nil, &out, true); err != nil {
		return nil, err
	}
	if !out.Changed {
		return nil, nil
	}
	return &out, nil
}

func (c *Client) PostGuestCalls(ctx context.Context, hostID string, calls []hostapi.GuestCall) error {
	_, err := c.do(ctx, http.MethodPost, fmt.Sprintf(hostapi.PathGuestCalls, hostID), nil,
		hostapi.GuestCallBatch{Calls: calls}, nil, true)
	return err
}

// PostEvents posts watchdog events. ErrNotSupported means the control plane
// has no events endpoint (state still reaches it via heartbeat and observed).
func (c *Client) PostEvents(ctx context.Context, hostID string, events []hostapi.HostEvent) error {
	code, err := c.do(ctx, http.MethodPost, fmt.Sprintf(hostapi.PathEvents, hostID), nil,
		hostapi.HostEventBatch{Events: events}, nil, true)
	if code == http.StatusNotFound || code == http.StatusMethodNotAllowed {
		return ErrNotSupported
	}
	return err
}
