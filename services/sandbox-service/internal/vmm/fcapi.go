package vmm

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"time"
)

// fcAPI is a minimal client for Firecracker's API over its Unix socket. Only
// the calls boss-hostd needs: pause/resume, snapshot create/load, MMDS.
type fcAPI struct{ sock string }

func (a fcAPI) client(timeout time.Duration) *http.Client {
	return &http.Client{Timeout: timeout, Transport: &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{Timeout: 2 * time.Second}).DialContext(ctx, "unix", a.sock)
		}}}
}

func (a fcAPI) do(ctx context.Context, method, path string, body any, timeout time.Duration) error {
	var rd io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rd = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, "http://fc"+path, rd)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.client(timeout).Do(req)
	if err != nil {
		return fmt.Errorf("firecracker api %s %s: %w", method, path, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 2000))
		return fmt.Errorf("firecracker api %s %s: %s: %s", method, path, resp.Status, bytes.TrimSpace(msg))
	}
	return nil
}

func (a fcAPI) pause(ctx context.Context) error {
	return a.do(ctx, http.MethodPatch, "/vm", map[string]string{"state": "Paused"}, 5*time.Second)
}

func (a fcAPI) resume(ctx context.Context) error {
	return a.do(ctx, http.MethodPatch, "/vm", map[string]string{"state": "Resumed"}, 5*time.Second)
}

// createSnapshot writes a full snapshot to the (chroot-relative) paths.
func (a fcAPI) createSnapshot(ctx context.Context, statePath, memPath string) error {
	return a.do(ctx, http.MethodPut, "/snapshot/create", map[string]any{
		"snapshot_type": "Full", "snapshot_path": statePath, "mem_file_path": memPath}, 60*time.Second)
}

// loadSnapshot restores from the (chroot-relative) paths and resumes. The
// memory file is mapped MAP_PRIVATE by Firecracker (File backend, not
// shared), so clean pages are shared copy-on-write between clones.
func (a fcAPI) loadSnapshot(ctx context.Context, statePath, memPath string) error {
	return a.do(ctx, http.MethodPut, "/snapshot/load", map[string]any{
		"snapshot_path":         statePath,
		"mem_backend":           map[string]string{"backend_type": "File", "backend_path": memPath},
		"enable_diff_snapshots": false, "resume_vm": true}, 60*time.Second)
}

// putMMDS replaces the MMDS data store (never persisted in a snapshot).
func (a fcAPI) putMMDS(ctx context.Context, data map[string]string) error {
	return a.do(ctx, http.MethodPut, "/mmds", data, 5*time.Second)
}

// ping reports whether the API socket answers.
func (a fcAPI) ping(ctx context.Context) error {
	return a.do(ctx, http.MethodGet, "/", nil, 2*time.Second)
}
