package lbd

import (
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
	"time"

	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
)

// DefaultPushPort is boss-lbd's mTLS listener for host pushes.
const DefaultPushPort = 7446

// PushServer accepts PUT/DELETE /v1/vips/{vip}/members/{replica} from hosts
// over mTLS (fleet-CA client certificates). A withdraw is applied to IPVS
// before the response is written; the latency from the first byte read to
// the kernel call returning is the `boss_lbd_withdraw_seconds` histogram.
type PushServer struct {
	Prog    *Programmer
	Metrics *Metrics
	Logf    func(string, ...any)
}

// Handler is the HTTP side (TLS and client-cert checks are the listener's).
func (s *PushServer) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/vips/", s.member)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	return mux
}

// hostIdentity admits only an enrolled host's own certificate (review of
// PR #120): issued directly by the fleet CA (never under a host's SVID
// issuer), not a CA, client auth and NOT server auth (a director's carries
// both), no VM SPIFFE id, and a CN the current desired state names as a
// live host. It returns the host id, or a reason.
func (s *PushServer) hostIdentity(r *http.Request) (string, string) {
	if r.TLS == nil || len(r.TLS.VerifiedChains) == 0 {
		return "", "a fleet client certificate is required"
	}
	chain := r.TLS.VerifiedChains[0]
	if len(chain) != 2 {
		return "", "the certificate must be issued directly by the fleet CA"
	}
	leaf := chain[0]
	if leaf.IsCA {
		return "", "a CA certificate cannot push"
	}
	client, server := false, false
	for _, u := range leaf.ExtKeyUsage {
		switch u {
		case x509.ExtKeyUsageClientAuth:
			client = true
		case x509.ExtKeyUsageServerAuth:
			server = true
		case x509.ExtKeyUsageAny:
			client, server = true, true
		}
	}
	if !client || server {
		return "", "only a host's client certificate may push (not a director's)"
	}
	for _, u := range leaf.URIs {
		if strings.Contains(u.Path, "/vm/") {
			return "", "a VM identity cannot push"
		}
	}
	id := leaf.Subject.CommonName
	if !s.Prog.HostKnown(id) {
		return "", "not an enrolled host in this director's desired state"
	}
	return id, ""
}

func (s *PushServer) member(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	rest := strings.Trim(strings.TrimPrefix(r.URL.Path, "/v1/vips/"), "/")
	parts := strings.Split(rest, "/")
	if len(parts) != 3 || parts[1] != "members" {
		http.NotFound(w, r)
		return
	}
	vip, replica := parts[0], parts[2]
	if !ValidVIP(vip) {
		writeErr(w, http.StatusBadRequest, "vip_invalid", "vip must be an IPv4 address")
		return
	}
	host, why := s.hostIdentity(r)
	if host == "" {
		if s.Metrics != nil {
			s.Metrics.Inc("boss_lbd_push_refused_total", 1)
		}
		writeErr(w, http.StatusForbidden, "host_not_allowed", why)
		return
	}
	var body hostapi.VIPMember
	switch r.Method {
	case http.MethodPut:
		if err := json.NewDecoder(io.LimitReader(r.Body, 64<<10)).Decode(&body); err != nil {
			writeErr(w, http.StatusBadRequest, "body_invalid", err.Error())
			return
		}
	case http.MethodDelete:
		body.VMIP = r.URL.Query().Get("vm_ip")
		body.Reason = r.URL.Query().Get("reason")
		body.Seq, _ = strconv.ParseInt(r.URL.Query().Get("seq"), 10, 64)
	default:
		writeErr(w, http.StatusMethodNotAllowed, "method", "PUT or DELETE")
		return
	}
	if net.ParseIP(body.VMIP).To4() == nil {
		writeErr(w, http.StatusBadRequest, "vm_ip_invalid", "vm_ip must be IPv4")
		return
	}
	// A host acts only on its own member: (vip, vm_ip) listed under its id.
	if owner := s.Prog.MemberOwner(vip, body.VMIP); owner != host {
		if s.Metrics != nil {
			s.Metrics.Inc("boss_lbd_push_refused_total", 1)
		}
		writeErr(w, http.StatusForbidden, "member_not_this_hosts",
			"this (vip, vm_ip) is not a member of this host in the desired state")
		return
	}
	var n int
	var err error
	kind := "withdraw"
	switch {
	case r.Method == http.MethodDelete:
		n, err = s.Prog.Withdraw(vip, body.VMIP, body.Ports, 0, true, body.Seq)
	case body.Weight <= 0:
		n, err = s.Prog.Withdraw(vip, body.VMIP, body.Ports, 0, false, body.Seq)
	default:
		kind = "register"
		n, err = s.Prog.Register(vip, body.VMIP, body.Ports, body.Weight, body.Seq)
	}
	if errors.Is(err, ErrStale) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"applied": 0, "stale": true, "vip": vip, "replica_id": replica})
		return
	}
	lat := time.Since(start)
	if s.Metrics != nil {
		s.Metrics.Inc("boss_lbd_push_total", 1)
		if kind == "withdraw" {
			s.Metrics.Observe("boss_lbd_withdraw_seconds", lat)
		}
		if err != nil {
			s.Metrics.Inc("boss_lbd_push_errors_total", 1)
		}
	}
	if s.Logf != nil {
		s.Logf("%s from %s: vip %s replica %s vm_ip %s weight %d reason %q → %d rs in %s (err=%v)", kind, host, vip,
			replica, body.VMIP, body.Weight, body.Reason, n, lat.Round(time.Microsecond), err)
	}
	out := map[string]any{"applied": n, "latency_us": lat.Microseconds(), "vip": vip, "replica_id": replica}
	if err != nil && n == 0 {
		out["error"] = err.Error()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusConflict) // nothing changed (member unknown here yet, or no such service)
		_ = json.NewEncoder(w).Encode(out)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(out)
}

func writeErr(w http.ResponseWriter, code int, reason, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]any{"detail": map[string]string{"error": reason, "message": msg}})
}

// ServerTLS is the listener's TLS: this director's fleet certificate, and
// client certificates verified against the fleet CA.
func ServerTLS(cert func() *tls.Certificate, fleetCA *x509.CertPool) *tls.Config {
	return &tls.Config{
		MinVersion: tls.VersionTLS12,
		ClientAuth: tls.RequireAndVerifyClientCert,
		ClientCAs:  fleetCA,
		GetCertificate: func(*tls.ClientHelloInfo) (*tls.Certificate, error) {
			c := cert()
			if c == nil {
				return nil, errors.New("director not enrolled")
			}
			return c, nil
		},
	}
}

// Pusher is the host side: one mTLS client per director, pushing weight 0,
// delete and register. Every push to every director runs in parallel with
// a short deadline; the control plane remains the durable record.
type Pusher struct {
	Cert    func() *tls.Certificate // the host's fleet client certificate
	FleetCA *x509.CertPool
	Timeout time.Duration
	Logf    func(string, ...any)
}

// PushResult is one director's answer.
type PushResult struct {
	Director string
	Latency  time.Duration
	Err      error
}

func (p *Pusher) client(expectID string) *http.Client {
	cfg := &tls.Config{
		MinVersion:         tls.VersionTLS12,
		InsecureSkipVerify: true, // replaced by VerifyPeerCertificate: fleet CA + the director's id
		GetClientCertificate: func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
			if c := p.Cert(); c != nil {
				return c, nil
			}
			return nil, errors.New("host not enrolled")
		},
		VerifyPeerCertificate: func(raw [][]byte, _ [][]*x509.Certificate) error {
			if len(raw) == 0 {
				return errors.New("no director certificate")
			}
			leaf, err := x509.ParseCertificate(raw[0])
			if err != nil {
				return err
			}
			inter := x509.NewCertPool()
			for _, r := range raw[1:] {
				if c, err := x509.ParseCertificate(r); err == nil {
					inter.AddCert(c)
				}
			}
			if _, err := leaf.Verify(x509.VerifyOptions{Roots: p.FleetCA, Intermediates: inter,
				KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageAny}}); err != nil {
				return fmt.Errorf("director certificate does not chain to the fleet CA: %w", err)
			}
			if expectID != "" && leaf.Subject.CommonName != expectID {
				return fmt.Errorf("director certificate is %q, expected %q", leaf.Subject.CommonName, expectID)
			}
			return nil
		},
	}
	return &http.Client{Timeout: p.Timeout, Transport: &http.Transport{TLSClientConfig: cfg, DisableKeepAlives: false,
		MaxIdleConnsPerHost: 2, IdleConnTimeout: 60 * time.Second}}
}

// Push sends one member call to every director and waits for all (bounded
// by Timeout, default 500 ms).
func (p *Pusher) Push(ctx context.Context, directors []hostapi.DirectorEndpoint, method, vip, replica string, m hostapi.VIPMember) []PushResult {
	if p.Timeout <= 0 {
		p.Timeout = 500 * time.Millisecond
	}
	out := make([]PushResult, len(directors))
	done := make(chan struct{}, len(directors))
	for i, d := range directors {
		go func(i int, d hostapi.DirectorEndpoint) {
			defer func() { done <- struct{}{} }()
			start := time.Now()
			out[i] = PushResult{Director: d.ID}
			port := d.Port
			if port == 0 {
				port = DefaultPushPort
			}
			u := url.URL{Scheme: "https", Host: net.JoinHostPort(d.Address, strconv.Itoa(port)),
				Path: fmt.Sprintf(hostapi.PathVIPMember, vip, url.PathEscape(replica))}
			var body io.Reader
			if method == http.MethodDelete {
				q := url.Values{"vm_ip": {m.VMIP}}
				if m.Reason != "" {
					q.Set("reason", m.Reason)
				}
				if m.Seq > 0 {
					q.Set("seq", strconv.FormatInt(m.Seq, 10))
				}
				u.RawQuery = q.Encode()
			} else {
				raw, _ := json.Marshal(m)
				body = strings.NewReader(string(raw))
			}
			cctx, cancel := context.WithTimeout(ctx, p.Timeout)
			defer cancel()
			req, err := http.NewRequestWithContext(cctx, method, u.String(), body)
			if err != nil {
				out[i].Err = err
				return
			}
			if body != nil {
				req.Header.Set("Content-Type", "application/json")
			}
			resp, err := p.client(d.ID).Do(req)
			if err != nil {
				out[i].Err = err
				return
			}
			_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
			resp.Body.Close()
			if resp.StatusCode/100 != 2 {
				out[i].Err = fmt.Errorf("director %s answered %d", d.ID, resp.StatusCode)
			}
			out[i].Latency = time.Since(start)
		}(i, d)
	}
	for range directors {
		<-done
	}
	return out
}
