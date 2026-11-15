package netpol

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"time"
)

// The CAP_NET_ADMIN helper boundary. boss-hostd runs unprivileged and asks
// the helper (boss-hostd netadm, started by systemd with
// AmbientCapabilities=CAP_NET_ADMIN, or by sudo on a dev box) to do exactly
// these operations, each re-validated on the privileged side:
//
//	ensure {net}  remove {tap}  taps  counters {tap}  directors {ips}  policy
//
// One JSON request line, one JSON reply line, per connection. The helper
// accepts only the configured peer uid (SO_PEERCRED).

type helperReq struct {
	Op  string   `json:"op"`
	Net *VMNet   `json:"net,omitempty"`
	Tap string   `json:"tap,omitempty"`
	IPs []string `json:"ips,omitempty"`
}

type helperResp struct {
	OK       bool      `json:"ok"`
	Error    string    `json:"error,omitempty"`
	Taps     []string  `json:"taps,omitempty"`
	Counters *Counters `json:"counters,omitempty"`
	PolicyOK bool      `json:"policy_ok"`
	Reason   string    `json:"reason,omitempty"`
}

// HelperClient is the unprivileged side; it implements Manager.
type HelperClient struct{ Socket string }

func (h HelperClient) call(ctx context.Context, req helperReq) (*helperResp, error) {
	d := net.Dialer{Timeout: 2 * time.Second}
	c, err := d.DialContext(ctx, "unix", h.Socket)
	if err != nil {
		return nil, fmt.Errorf("netadm helper unreachable: %w", err)
	}
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(20 * time.Second))
	raw, _ := json.Marshal(req)
	if _, err := c.Write(append(raw, '\n')); err != nil {
		return nil, err
	}
	line, err := bufio.NewReader(c).ReadBytes('\n')
	if err != nil {
		return nil, err
	}
	var resp helperResp
	if err := json.Unmarshal(line, &resp); err != nil {
		return nil, err
	}
	if !resp.OK {
		return &resp, errors.New(resp.Error)
	}
	return &resp, nil
}

func (h HelperClient) Ensure(ctx context.Context, v VMNet) error {
	_, err := h.call(ctx, helperReq{Op: "ensure", Net: &v})
	return err
}
func (h HelperClient) Remove(ctx context.Context, tap string) error {
	_, err := h.call(ctx, helperReq{Op: "remove", Tap: tap})
	return err
}
func (h HelperClient) Taps(ctx context.Context) ([]string, error) {
	r, err := h.call(ctx, helperReq{Op: "taps"})
	if err != nil {
		return nil, err
	}
	return r.Taps, nil
}
func (h HelperClient) Counters(ctx context.Context, tap string) (Counters, error) {
	r, err := h.call(ctx, helperReq{Op: "counters", Tap: tap})
	if err != nil || r.Counters == nil {
		return Counters{}, err
	}
	return *r.Counters, nil
}
func (h HelperClient) SetDirectors(ctx context.Context, ips []string) error {
	_, err := h.call(ctx, helperReq{Op: "directors", IPs: ips})
	return err
}
func (h HelperClient) PolicyOK(ctx context.Context) (bool, string) {
	r, err := h.call(ctx, helperReq{Op: "policy"})
	if err != nil {
		return false, err.Error()
	}
	return r.PolicyOK, r.Reason
}

// Dispatch runs one validated request against a privileged Manager. The
// helper server calls it; it is separate so the validation is unit-testable.
func Dispatch(ctx context.Context, m Manager, req helperReq) helperResp {
	fail := func(err error) helperResp { return helperResp{Error: err.Error()} }
	switch req.Op {
	case "ensure":
		if req.Net == nil {
			return fail(errors.New("ensure: net required"))
		}
		if err := req.Net.Validate(); err != nil {
			return fail(err)
		}
		if err := m.Ensure(ctx, *req.Net); err != nil {
			return fail(err)
		}
		return helperResp{OK: true}
	case "remove":
		if !ValidTap(req.Tap) {
			return fail(fmt.Errorf("remove: bad tap %q", req.Tap))
		}
		if err := m.Remove(ctx, req.Tap); err != nil {
			return fail(err)
		}
		return helperResp{OK: true}
	case "taps":
		taps, err := m.Taps(ctx)
		if err != nil {
			return fail(err)
		}
		return helperResp{OK: true, Taps: taps}
	case "counters":
		if !ValidTap(req.Tap) {
			return fail(fmt.Errorf("counters: bad tap %q", req.Tap))
		}
		c, err := m.Counters(ctx, req.Tap)
		if err != nil {
			return fail(err)
		}
		return helperResp{OK: true, Counters: &c}
	case "directors":
		if _, err := RenderDirectors(req.IPs); err != nil {
			return fail(err)
		}
		if err := m.SetDirectors(ctx, req.IPs); err != nil {
			return fail(err)
		}
		return helperResp{OK: true}
	case "policy":
		ok, reason := m.PolicyOK(ctx)
		return helperResp{OK: true, PolicyOK: ok, Reason: reason}
	default:
		return fail(fmt.Errorf("unknown op %q", req.Op))
	}
}
