// Package dispatch is the BOSS-102 S3 fan-out worker.
//
// It consumes a spec-00 event envelope and POSTs to configured webhook URLs.
// The Python engine remains the event source of truth. This package never
// invents success, never silently drops a destination, and never logs raw secrets.
package dispatch

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"
)

const (
	StatusSucceeded    = "succeeded"
	StatusFailed       = "failed"
	StatusDeadlettered = "deadlettered"
	StatusDryRun       = "dry_run"

	EffectKind = "webhook.go_dispatcher.deliver"
)

// Config controls retry, dry-run, and secret resolution.
type Config struct {
	DryRun      bool
	MaxAttempts int
	Backoff     time.Duration
	Timeout     time.Duration
	LookupEnv   func(string) (string, bool)
	Sleep       func(time.Duration)
	Now         func() time.Time
	Log         io.Writer
}

func (c Config) maxAttempts() int {
	if c.MaxAttempts < 1 {
		return 3
	}
	return c.MaxAttempts
}

func (c Config) sleep() func(time.Duration) {
	if c.Sleep != nil {
		return c.Sleep
	}
	return time.Sleep
}

func (c Config) now() time.Time {
	if c.Now != nil {
		return c.Now()
	}
	return time.Now().UTC()
}

func (c Config) lookup(name string) (string, bool) {
	if c.LookupEnv != nil {
		return c.LookupEnv(name)
	}
	return "", false
}

// Destination is one webhook URL. SecretRef is an env:NAME pointer, never a raw secret.
type Destination struct {
	ID           string `json:"id"`
	URL          string `json:"url"`
	SecretRef    string `json:"secret_ref"`
	DataBoundary string `json:"data_boundary"`
}

// Envelope is the spec-00 event payload the Python SoR already created.
type Envelope struct {
	EventID      string         `json:"event_id"`
	EventType    string         `json:"event_type"`
	ProjectID    string         `json:"project_id"`
	Actor        string         `json:"actor"`
	EntityType   string         `json:"entity_type"`
	EntityID     string         `json:"entity_id"`
	Data         map[string]any `json:"data"`
	Destinations []Destination  `json:"destinations"`
	DryRun       bool           `json:"dry_run"`
}

// Receipt is required for every destination attempt, including dry-run and failure.
type Receipt struct {
	ID            string `json:"id"`
	DedupeKey     string `json:"dedupe_key"`
	DestinationID string `json:"destination_id"`
	Status        string `json:"status"`
	Attempt       int    `json:"attempt"`
	HTTPStatus    int    `json:"http_status,omitempty"`
	Error         string `json:"error,omitempty"`
	EffectKind    string `json:"effect_kind"`
}

// Result is the fan-out outcome. Dropped is always false on a completed Dispatch.
type Result struct {
	EventID  string    `json:"event_id"`
	DryRun   bool      `json:"dry_run"`
	Dropped  bool      `json:"dropped"`
	Receipts []Receipt `json:"receipts"`
}

// Poster performs the outbound HTTP POST. Tests inject a fake; live uses HTTPPoster.
type Poster interface {
	Post(url string, body []byte, headers map[string]string) (status int, respBody string, err error)
}

// PosterFunc adapts a function to Poster.
type PosterFunc func(url string, body []byte, headers map[string]string) (int, string, error)

func (f PosterFunc) Post(url string, body []byte, headers map[string]string) (int, string, error) {
	return f(url, body, headers)
}

// Store remembers receipts by dedupe_key so a replay cannot double-deliver.
type Store interface {
	Get(dedupeKey string) (Receipt, bool)
	Put(Receipt)
}

// MemoryStore is the smallest durable-enough store for a single process.
type MemoryStore struct {
	mu    sync.Mutex
	items map[string]Receipt
}

func NewMemoryStore() *MemoryStore {
	return &MemoryStore{items: map[string]Receipt{}}
}

func (s *MemoryStore) Get(dedupeKey string) (Receipt, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	rcpt, ok := s.items[dedupeKey]
	return rcpt, ok
}

func (s *MemoryStore) Put(rcpt Receipt) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.items[rcpt.DedupeKey] = rcpt
}

// Dispatcher fans one envelope out to many destinations.
type Dispatcher struct {
	cfg    Config
	store  Store
	poster Poster
}

func New(cfg Config, store Store, poster Poster) *Dispatcher {
	if store == nil {
		store = NewMemoryStore()
	}
	if poster == nil {
		poster = HTTPPoster{Timeout: cfg.Timeout}
	}
	return &Dispatcher{cfg: cfg, store: store, poster: poster}
}

// DedupeKey is unique per (project, event, destination).
func DedupeKey(projectID, eventID, destID string) string {
	return fmt.Sprintf("webhook-go:%s:%s:%s", projectID, eventID, destID)
}

// Sign returns GitHub-compatible X-Hub-Signature-256 over the exact body bytes.
func Sign(secret, body []byte) string {
	mac := hmac.New(sha256.New, secret)
	mac.Write(body)
	return "sha256=" + hex.EncodeToString(mac.Sum(nil))
}

func CanonicalBody(env Envelope, dest Destination, deliveryID string) ([]byte, error) {
	payload := struct {
		Data       any    `json:"data"`
		DeliveryID string `json:"delivery_id"`
		EventID    string `json:"event_id"`
		EventType  string `json:"event_type"`
	}{
		Data:       env.Data,
		DeliveryID: deliveryID,
		EventID:    env.EventID,
		EventType:  env.EventType,
	}
	if payload.Data == nil {
		payload.Data = map[string]any{}
	}
	return json.Marshal(payload)
}

func newID() string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return "whgo_" + hex.EncodeToString(b[:])
}

func (d *Dispatcher) log(msg string, fields map[string]any) {
	if d.cfg.Log == nil {
		return
	}
	safe := map[string]any{}
	for k, v := range fields {
		safe[k] = redactValue(fmt.Sprint(v))
	}
	line, _ := json.Marshal(struct {
		Msg    string         `json:"msg"`
		Fields map[string]any `json:"fields"`
	}{Msg: msg, Fields: safe})
	fmt.Fprintln(d.cfg.Log, string(line))
}

func redactValue(s string) string {
	lower := strings.ToLower(s)
	if strings.Contains(lower, "sha256=") || strings.Contains(s, "env:") {
		return "[REDACTED]"
	}
	return s
}

func resolveSecret(ref string, lookup func(string) (string, bool)) (string, error) {
	ref = strings.TrimSpace(ref)
	if !strings.HasPrefix(ref, "env:") {
		return "", fmt.Errorf("secret_ref must be env:NAME, never a raw secret")
	}
	name := strings.TrimSpace(ref[4:])
	if name == "" {
		return "", fmt.Errorf("secret_ref env name is empty")
	}
	value, ok := lookup(name)
	if !ok || value == "" {
		return "", fmt.Errorf("webhook secret reference env:%s is unavailable", name)
	}
	return value, nil
}

// Dispatch fans the envelope out. Every destination yields a Receipt.
func (d *Dispatcher) Dispatch(env Envelope) (Result, error) {
	dry := d.cfg.DryRun || env.DryRun
	result := Result{EventID: env.EventID, DryRun: dry, Dropped: false}
	if strings.TrimSpace(env.EventID) == "" {
		return result, fmt.Errorf("event_id is required")
	}
	if len(env.Destinations) == 0 {
		rcpt := Receipt{
			ID:            newID(),
			DedupeKey:     DedupeKey(env.ProjectID, env.EventID, "none"),
			DestinationID: "none",
			Status:        StatusDeadlettered,
			Attempt:       0,
			Error:         "no destinations configured; fail-visible, not a silent drop",
			EffectKind:    EffectKind,
		}
		d.store.Put(rcpt)
		result.Receipts = []Receipt{rcpt}
		d.log("dispatch.no_destinations", map[string]any{
			"event_id":   env.EventID,
			"project_id": env.ProjectID,
		})
		return result, nil
	}
	for _, dest := range env.Destinations {
		result.Receipts = append(result.Receipts, d.deliverOne(env, dest, dry))
	}
	return result, nil
}

func (d *Dispatcher) deliverOne(env Envelope, dest Destination, dry bool) Receipt {
	key := DedupeKey(env.ProjectID, env.EventID, dest.ID)
	if existing, ok := d.store.Get(key); ok {
		d.log("dispatch.deduped", map[string]any{
			"event_id":        env.EventID,
			"destination_id":  dest.ID,
			"dedupe_key":      key,
			"existing_status": existing.Status,
		})
		return existing
	}
	rcpt := Receipt{
		ID:            newID(),
		DedupeKey:     key,
		DestinationID: dest.ID,
		EffectKind:    EffectKind,
	}
	if dry {
		rcpt.Status = StatusDryRun
		rcpt.Attempt = 0
		d.store.Put(rcpt)
		d.log("dispatch.dry_run", map[string]any{
			"event_id":       env.EventID,
			"destination_id": dest.ID,
			"receipt_id":     rcpt.ID,
		})
		return rcpt
	}
	if strings.TrimSpace(dest.URL) == "" {
		rcpt.Status = StatusDeadlettered
		rcpt.Error = "destination url is empty"
		d.store.Put(rcpt)
		return rcpt
	}
	secret, err := resolveSecret(dest.SecretRef, d.cfg.lookup)
	if err != nil {
		rcpt.Status = StatusDeadlettered
		rcpt.Error = err.Error()
		d.store.Put(rcpt)
		d.log("dispatch.secret_unavailable", map[string]any{
			"event_id":       env.EventID,
			"destination_id": dest.ID,
			"secret_ref":     dest.SecretRef,
		})
		return rcpt
	}
	body, err := CanonicalBody(env, dest, rcpt.ID)
	if err != nil {
		rcpt.Status = StatusDeadlettered
		rcpt.Error = err.Error()
		d.store.Put(rcpt)
		return rcpt
	}
	signature := Sign([]byte(secret), body)
	headers := map[string]string{
		"Content-Type":         "application/json",
		"X-Boss-Event":         env.EventType,
		"X-Boss-Delivery":      rcpt.ID,
		"X-Hub-Signature-256":  signature,
	}
	max := d.cfg.maxAttempts()
	var lastErr string
	var lastStatus int
	for attempt := 1; attempt <= max; attempt++ {
		rcpt.Attempt = attempt
		status, _, postErr := d.poster.Post(dest.URL, body, headers)
		lastStatus = status
		if postErr == nil && status >= 200 && status < 300 {
			rcpt.Status = StatusSucceeded
			rcpt.HTTPStatus = status
			rcpt.Error = ""
			d.store.Put(rcpt)
			d.log("dispatch.delivered", map[string]any{
				"event_id":       env.EventID,
				"destination_id": dest.ID,
				"receipt_id":     rcpt.ID,
				"attempt":        attempt,
				"http_status":    status,
			})
			return rcpt
		}
		if postErr != nil {
			lastErr = postErr.Error()
		} else {
			lastErr = fmt.Sprintf("webhook endpoint returned HTTP %d", status)
		}
		d.log("dispatch.attempt_failed", map[string]any{
			"event_id":       env.EventID,
			"destination_id": dest.ID,
			"receipt_id":     rcpt.ID,
			"attempt":        attempt,
			"http_status":    status,
			"error":          lastErr,
		})
		if attempt < max && d.cfg.Backoff >= 0 {
			delay := d.cfg.Backoff
			if attempt > 1 && delay > 0 {
				delay = d.cfg.Backoff * time.Duration(1<<uint(attempt-1))
			}
			d.cfg.sleep()(delay)
		}
	}
	rcpt.Status = StatusDeadlettered
	rcpt.HTTPStatus = lastStatus
	rcpt.Error = lastErr
	d.store.Put(rcpt)
	d.log("dispatch.deadlettered", map[string]any{
		"event_id":       env.EventID,
		"destination_id": dest.ID,
		"receipt_id":     rcpt.ID,
		"attempts":       rcpt.Attempt,
	})
	return rcpt
}

// HTTPPoster is the live network poster. Tests must not use it when DryRun is set.
type HTTPPoster struct {
	Timeout time.Duration
	Client  *http.Client
}

func (p HTTPPoster) Post(url string, body []byte, headers map[string]string) (int, string, error) {
	timeout := p.Timeout
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	client := p.Client
	if client == nil {
		client = &http.Client{Timeout: timeout}
	}
	req, err := http.NewRequest(http.MethodPost, url, strings.NewReader(string(body)))
	if err != nil {
		return 0, "", err
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := client.Do(req)
	if err != nil {
		return 0, "", err
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 2000))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return resp.StatusCode, string(respBody), fmt.Errorf("webhook endpoint returned HTTP %d", resp.StatusCode)
	}
	return resp.StatusCode, string(respBody), nil
}

// NewHandler serves /health and POST /v1/dispatch.
func NewHandler(d *Dispatcher, token string) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok","service":"webhook-dispatcher","ga":false}`))
	})
	mux.HandleFunc("/v1/dispatch", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if token != "" && r.Header.Get("X-Boss-Dispatcher-Token") != token {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		var env Envelope
		if err := json.NewDecoder(r.Body).Decode(&env); err != nil {
			http.Error(w, "invalid envelope", http.StatusBadRequest)
			return
		}
		result, err := d.Dispatch(env)
		if err != nil {
			http.Error(w, err.Error(), http.StatusUnprocessableEntity)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(result)
	})
	return mux
}
