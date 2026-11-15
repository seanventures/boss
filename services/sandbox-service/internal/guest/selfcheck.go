package guest

import (
	"context"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// SelfcheckEgress tries to originate traffic the network policy must stop
// and reports what happened. It sends no data that matters: a 1-byte UDP
// datagram and two TCP SYNs. On a replica every attempt must die at the host
// tap (and show in the drop counter); on a per_run VM there is no route at
// all. Used by the P8a-T4 evidence and as a host attestation probe.
func SelfcheckEgress() map[string]string {
	out := map[string]string{}
	try := func(name, network, addr string) {
		c, err := net.DialTimeout(network, addr, 400*time.Millisecond)
		if err != nil {
			out[name] = "blocked: " + err.Error()
			return
		}
		defer c.Close()
		if network == "udp" {
			_ = c.SetDeadline(time.Now().Add(400 * time.Millisecond))
			if _, err := c.Write([]byte{0}); err != nil {
				out[name] = "blocked: " + err.Error()
				return
			}
			buf := make([]byte, 1)
			if _, err := c.Read(buf); err != nil {
				out[name] = "sent-no-reply"
				return
			}
			out[name] = "REPLY"
			return
		}
		out[name] = "CONNECTED"
	}
	try("udp_dns_8.8.8.8", "udp", "8.8.8.8:53")
	try("tcp_metadata_169.254.169.254", "tcp", "169.254.169.254:80")
	try("tcp_internet_1.1.1.1", "tcp", "1.1.1.1:443")
	ctx, cancel := context.WithTimeout(context.Background(), 600*time.Millisecond)
	defer cancel()
	if _, err := net.DefaultResolver.LookupHost(ctx, "example.com"); err != nil {
		out["dns_lookup"] = "blocked: " + err.Error()
	} else {
		out["dns_lookup"] = "RESOLVED"
	}
	return out
}

// SelfcheckGateway makes one run-plane call through the local relay exactly
// as the in-VM runtime will: plain HTTP to 127.0.0.1 with its lease header,
// plus a guest credential and forged identity headers (X-Boss-Replica,
// X-Boss-Run-Id, Cookie) the host gateway must strip or replace. It reports
// the status and what the upstream says it received.
func SelfcheckGateway(port int, handle string) map[string]string {
	out := map[string]string{}
	if port == 0 {
		out["error"] = "no run-plane relay port"
		return out
	}
	req, _ := http.NewRequest(http.MethodPost, "http://127.0.0.1:"+strconv.Itoa(port)+"/v1/run-plane/selfcheck",
		strings.NewReader(`{"probe":true}`))
	req.Header.Set("Content-Type", "application/json")
	if handle != "" {
		req.Header.Set("Authorization", "Bearer "+handle)
	} else {
		req.Header.Set("Authorization", "Bearer guest-supplied-credential")
	}
	req.Header.Set("X-Boss-Run-Id", "forged-by-guest")
	req.Header.Set("X-Boss-Replica", "forged-replica")
	req.Header.Set("X-Boss-Job", "job_selfcheck") // lease data: must pass through
	req.Header.Set("Cookie", "session=forged")
	c := &http.Client{Timeout: 5 * time.Second}
	resp, err := c.Do(req)
	if err != nil {
		out["error"] = err.Error()
		return out
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	out["status"] = strconv.Itoa(resp.StatusCode)
	out["body"] = string(body)
	return out
}
