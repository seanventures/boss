package config

import (
	"encoding/json"
	"log"
	"os"
	"path/filepath"
	"time"

	"northgate/internal/metrics"
)

// ReloadOnce performs a single synchronous reload attempt: load and
// validate dir's config, swap it into store on success (keeping the last
// good snapshot on failure), update state for /readyz, and record the
// outcome as gateway_config_reload_total{result="applied"|"rejected"}.
func ReloadOnce(dir string, store *Store, state *ReloadState, reg *metrics.Registry) error {
	snap, err := Load(dir)
	if err != nil {
		state.MarkRejected(err.Error())
		if reg != nil {
			reg.IncCounter("gateway_config_reload_total", "Config hot-reload attempts by result", map[string]string{"result": "rejected"})
		}
		logReloadRejected(dir, err)
		return err
	}
	store.Swap(snap)
	state.MarkLoaded()
	if reg != nil {
		reg.IncCounter("gateway_config_reload_total", "Config hot-reload attempts by result", map[string]string{"result": "applied"})
	}
	return nil
}

// Watch polls dir (and dir/tenants) for file mtime changes (not size) every interval,
// debounces, and on change calls ReloadOnce and reports the result via
// onReload. A failing reload keeps the previous Store contents untouched.
// The caller is expected to have already loaded dir once (e.g. via
// config.Load/store.Swap at startup); Watch seeds lastSig from the
// directory's current signature so that startup state isn't treated as a
// change and reloaded (and re-counted) again on the first tick.
func Watch(dir string, interval time.Duration, store *Store, state *ReloadState, reg *metrics.Registry, onReload func(snap *Snapshot, err error)) {
	lastSig := dirSignature(dir)
	for {
		sig := dirSignature(dir)
		if sig != lastSig {
			lastSig = sig
			if err := ReloadOnce(dir, store, state, reg); err != nil {
				onReload(nil, err)
			} else {
				onReload(store.Current(), nil)
			}
		}
		time.Sleep(interval)
	}
}

// logReloadRejected emits a structured JSON log line for a rejected reload
// so the error detail is machine-parseable (e.g. by a log aggregator)
// rather than only readable as free-form text.
func logReloadRejected(dir string, err error) {
	b, mErr := json.Marshal(map[string]any{
		"event":  "config_reload_rejected",
		"dir":    dir,
		"error":  err.Error(),
		"result": "rejected",
	})
	if mErr != nil {
		log.Printf("config reload rejected: %v (keeping last good config)", err)
		return
	}
	log.Println(string(b))
}

func dirSignature(dir string) string {
	sig := ""
	filepath.Walk(dir, func(path string, info os.FileInfo, err error) error {
		if err != nil || info == nil || info.IsDir() {
			return nil
		}
		sig += path + info.ModTime().String() + "|"
		return nil
	})
	return sig
}
