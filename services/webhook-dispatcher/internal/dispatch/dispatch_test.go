package dispatch

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

const testSecret = "super-secret-webhook-key-do-not-log"

func testEnvelope(dests ...Destination) Envelope {
	return Envelope{
		EventID:      "evt_test_1",
		EventType:    "code_task.completed",
		ProjectID:    "boss-dogfood",
		Actor:        "system",
		EntityType:   "code_task",
		EntityID:     "task_1",
		Data:         map[string]any{"ok": true},
		Destinations: dests,
	}
}

func dest(id, url string) Destination {
	return Destination{
		ID:           id,
		URL:          url,
		SecretRef:    "env:BOSS_TEST_WEBHOOK_SECRET",
		DataBoundary: "internal",
	}
}

func envLookup(t *testing.T) func(string) (string, bool) {
	t.Helper()
	return func(name string) (string, bool) {
		if name == "BOSS_TEST_WEBHOOK_SECRET" {
			return testSecret, true
		}
		return "", false
	}
}

func newDispatcher(t *testing.T, poster Poster, logs *bytes.Buffer, dryRun bool, attempts int) *Dispatcher {
	t.Helper()
	if attempts < 1 {
		attempts = 3
	}
	return New(Config{
		DryRun:      dryRun,
		MaxAttempts: attempts,
		Backoff:     0,
		Timeout:     time.Second,
		LookupEnv:   envLookup(t),
		Sleep:       func(time.Duration) {},
		Now:         func() time.Time { return time.Unix(1_700_000_000, 0).UTC() },
		Log:         logs,
	}, NewMemoryStore(), poster)
}

func TestDedupeKeyIsUniquePerDestination(t *testing.T) {
	a := DedupeKey("boss-dogfood", "evt_1", "whend_a")
	b := DedupeKey("boss-dogfood", "evt_1", "whend_b")
	replay := DedupeKey("boss-dogfood", "evt_1", "whend_a")
	if a == "" || b == "" {
		t.Fatal("dedupe keys must not be empty")
	}
	if a == b {
		t.Fatalf("destinations must not share a dedupe_key: %s", a)
	}
	if a != replay {
		t.Fatalf("same destination must reuse dedupe_key: %s vs %s", a, replay)
	}
	if !strings.Contains(a, "evt_1") || !strings.Contains(a, "whend_a") {
		t.Fatalf("dedupe_key should bind event and destination: %s", a)
	}
}

func TestFanoutThenReplayDoesNotDoubleDeliver(t *testing.T) {
	var mu sync.Mutex
	var calls []string
	poster := PosterFunc(func(url string, body []byte, headers map[string]string) (int, string, error) {
		mu.Lock()
		defer mu.Unlock()
		calls = append(calls, url)
		if headers["X-Hub-Signature-256"] == "" {
			t.Errorf("missing HMAC header for %s", url)
		}
		return 204, "", nil
	})
	d := newDispatcher(t, poster, &bytes.Buffer{}, false, 1)
	env := testEnvelope(
		dest("whend_a", "https://a.example/hook"),
		dest("whend_b", "https://b.example/hook"),
	)

	first, err := d.Dispatch(env)
	if err != nil {
		t.Fatalf("first dispatch: %v", err)
	}
	if first.Dropped {
		t.Fatal("must not silently drop")
	}
	if len(first.Receipts) != 2 {
		t.Fatalf("want 2 receipts, got %d", len(first.Receipts))
	}
	keys := map[string]struct{}{}
	for _, rcpt := range first.Receipts {
		if rcpt.Status != StatusSucceeded {
			t.Fatalf("receipt %s status=%s error=%s", rcpt.ID, rcpt.Status, rcpt.Error)
		}
		if rcpt.DedupeKey == "" || rcpt.ID == "" {
			t.Fatal("every delivery needs a receipt id and dedupe_key")
		}
		if _, exists := keys[rcpt.DedupeKey]; exists {
			t.Fatalf("duplicate dedupe_key %s", rcpt.DedupeKey)
		}
		keys[rcpt.DedupeKey] = struct{}{}
	}

	second, err := d.Dispatch(env)
	if err != nil {
		t.Fatalf("replay dispatch: %v", err)
	}
	if len(second.Receipts) != 2 {
		t.Fatalf("replay must still return receipts, got %d", len(second.Receipts))
	}
	for _, rcpt := range second.Receipts {
		if rcpt.Status != StatusSucceeded {
			t.Fatalf("replay must reuse success, got %s", rcpt.Status)
		}
		if _, ok := keys[rcpt.DedupeKey]; !ok {
			t.Fatalf("replay invented a new dedupe_key %s", rcpt.DedupeKey)
		}
	}
	mu.Lock()
	defer mu.Unlock()
	if len(calls) != 2 {
		t.Fatalf("replay must not call the network again; calls=%v", calls)
	}
}

func TestRetryThenFailVisibleNeverInventSuccess(t *testing.T) {
	var calls int
	poster := PosterFunc(func(url string, body []byte, headers map[string]string) (int, string, error) {
		calls++
		return 503, "unavailable", errors.New("upstream 503")
	})
	d := newDispatcher(t, poster, &bytes.Buffer{}, false, 2)
	result, err := d.Dispatch(testEnvelope(dest("whend_fail", "https://fail.example/hook")))
	if err != nil {
		t.Fatalf("dispatch error: %v", err)
	}
	if len(result.Receipts) != 1 {
		t.Fatalf("want 1 receipt, got %+v", result.Receipts)
	}
	rcpt := result.Receipts[0]
	if rcpt.Status == StatusSucceeded || rcpt.Status == StatusDryRun {
		t.Fatalf("must not invent success: %+v", rcpt)
	}
	if rcpt.Status != StatusDeadlettered {
		t.Fatalf("exhausted retries must be deadlettered, got %s", rcpt.Status)
	}
	if rcpt.Attempt != 2 {
		t.Fatalf("want 2 attempts, got %d", rcpt.Attempt)
	}
	if rcpt.Error == "" {
		t.Fatal("fail-visible receipt must carry an error")
	}
	if rcpt.DedupeKey == "" {
		t.Fatal("failed delivery still needs a dedupe_key")
	}
	if calls != 2 {
		t.Fatalf("want 2 poster calls, got %d", calls)
	}
}

func TestRetryThenSucceedOnLaterAttempt(t *testing.T) {
	var calls int
	poster := PosterFunc(func(url string, body []byte, headers map[string]string) (int, string, error) {
		calls++
		if calls < 2 {
			return 500, "retry", errors.New("transient")
		}
		return 200, "ok", nil
	})
	d := newDispatcher(t, poster, &bytes.Buffer{}, false, 3)
	result, err := d.Dispatch(testEnvelope(dest("whend_ok", "https://ok.example/hook")))
	if err != nil {
		t.Fatalf("dispatch: %v", err)
	}
	if result.Receipts[0].Status != StatusSucceeded {
		t.Fatalf("want succeeded after retry, got %+v", result.Receipts[0])
	}
	if result.Receipts[0].Attempt != 2 {
		t.Fatalf("want attempt=2, got %d", result.Receipts[0].Attempt)
	}
}

func TestHMACMatchesIndependentSha256(t *testing.T) {
	var captured []byte
	var capturedSig string
	poster := PosterFunc(func(url string, body []byte, headers map[string]string) (int, string, error) {
		captured = append([]byte(nil), body...)
		capturedSig = headers["X-Hub-Signature-256"]
		return 204, "", nil
	})
	d := newDispatcher(t, poster, &bytes.Buffer{}, false, 1)
	env := testEnvelope(dest("whend_sig", "https://sig.example/hook"))
	result, err := d.Dispatch(env)
	if err != nil {
		t.Fatalf("dispatch: %v", err)
	}
	mac := hmac.New(sha256.New, []byte(testSecret))
	mac.Write(captured)
	want := "sha256=" + hex.EncodeToString(mac.Sum(nil))
	if capturedSig != want {
		t.Fatalf("HMAC mismatch:\n got %s\nwant %s\nbody %s", capturedSig, want, captured)
	}
	if result.Receipts[0].Status != StatusSucceeded {
		t.Fatalf("signed delivery failed: %+v", result.Receipts[0])
	}
	var payload map[string]any
	if err := json.Unmarshal(captured, &payload); err != nil {
		t.Fatalf("body is not JSON: %v", err)
	}
	if payload["event_type"] != "code_task.completed" {
		t.Fatalf("canonical body missing event_type: %v", payload)
	}
}

func TestLogsNeverContainRawSecretOrSignature(t *testing.T) {
	logs := &bytes.Buffer{}
	poster := PosterFunc(func(url string, body []byte, headers map[string]string) (int, string, error) {
		return 204, "", nil
	})
	d := newDispatcher(t, poster, logs, false, 1)
	if _, err := d.Dispatch(testEnvelope(dest("whend_log", "https://log.example/hook"))); err != nil {
		t.Fatalf("dispatch: %v", err)
	}
	out := logs.String()
	if out == "" {
		t.Fatal("expected some log output to assert redaction against")
	}
	if strings.Contains(out, testSecret) {
		t.Fatalf("raw secret leaked into logs:\n%s", out)
	}
	if strings.Contains(out, "sha256=") {
		t.Fatalf("HMAC signature leaked into logs:\n%s", out)
	}
}

func TestDryRunNeverCallsNetworkAndStillEmitsReceipts(t *testing.T) {
	poster := PosterFunc(func(url string, body []byte, headers map[string]string) (int, string, error) {
		t.Fatalf("dry-run must never call the network; hit %s", url)
		return 0, "", errors.New("unreachable")
	})
	d := newDispatcher(t, poster, &bytes.Buffer{}, true, 3)
	result, err := d.Dispatch(testEnvelope(
		dest("whend_dry_a", "https://dry-a.example/hook"),
		dest("whend_dry_b", "https://dry-b.example/hook"),
	))
	if err != nil {
		t.Fatalf("dispatch: %v", err)
	}
	if !result.DryRun {
		t.Fatal("result.DryRun must be true")
	}
	if len(result.Receipts) != 2 {
		t.Fatalf("dry-run still fans out receipts, got %d", len(result.Receipts))
	}
	seen := map[string]struct{}{}
	for _, rcpt := range result.Receipts {
		if rcpt.Status != StatusDryRun {
			t.Fatalf("dry-run receipt status=%s", rcpt.Status)
		}
		if rcpt.DedupeKey == "" || rcpt.ID == "" {
			t.Fatal("dry-run still needs receipt + dedupe_key")
		}
		seen[rcpt.DedupeKey] = struct{}{}
	}
	if len(seen) != 2 {
		t.Fatal("dry-run destinations must keep unique dedupe keys")
	}
}

func TestEnvelopeDryRunFlagOverridesLiveConfig(t *testing.T) {
	poster := PosterFunc(func(url string, body []byte, headers map[string]string) (int, string, error) {
		t.Fatal("envelope dry_run must suppress network")
		return 0, "", errors.New("unreachable")
	})
	d := newDispatcher(t, poster, &bytes.Buffer{}, false, 1)
	env := testEnvelope(dest("whend_flag", "https://flag.example/hook"))
	env.DryRun = true
	result, err := d.Dispatch(env)
	if err != nil {
		t.Fatalf("dispatch: %v", err)
	}
	if !result.DryRun || result.Receipts[0].Status != StatusDryRun {
		t.Fatalf("expected dry_run receipts, got %+v", result)
	}
}

func TestMissingSecretIsDeadletteredNotSuccess(t *testing.T) {
	d := New(Config{
		MaxAttempts: 1,
		LookupEnv:   func(string) (string, bool) { return "", false },
		Sleep:       func(time.Duration) {},
	}, NewMemoryStore(), PosterFunc(func(string, []byte, map[string]string) (int, string, error) {
		t.Fatal("must not POST without a resolved secret")
		return 0, "", errors.New("unreachable")
	}))
	result, err := d.Dispatch(testEnvelope(dest("whend_nosecret", "https://x.example/hook")))
	if err != nil {
		t.Fatalf("dispatch: %v", err)
	}
	if result.Receipts[0].Status != StatusDeadlettered {
		t.Fatalf("missing secret must fail closed, got %+v", result.Receipts[0])
	}
}

func TestNoDestinationsIsVisibleFailureNotDrop(t *testing.T) {
	d := newDispatcher(t, PosterFunc(func(string, []byte, map[string]string) (int, string, error) {
		t.Fatal("no destinations means no POST")
		return 0, "", nil
	}), &bytes.Buffer{}, false, 1)
	result, err := d.Dispatch(testEnvelope())
	if err != nil {
		t.Fatalf("dispatch: %v", err)
	}
	if result.Dropped {
		t.Fatal("empty destination list must not be a silent drop")
	}
	if len(result.Receipts) != 1 || result.Receipts[0].Status != StatusDeadlettered {
		t.Fatalf("want one fail-visible receipt, got %+v", result.Receipts)
	}
}

func TestHTTPHandlerHealthAndDispatch(t *testing.T) {
	poster := PosterFunc(func(url string, body []byte, headers map[string]string) (int, string, error) {
		return 204, "", nil
	})
	d := newDispatcher(t, poster, &bytes.Buffer{}, false, 1)
	handler := NewHandler(d, "disp-token")

	health := httptest.NewRecorder()
	handler.ServeHTTP(health, httptest.NewRequest(http.MethodGet, "/health", nil))
	if health.Code != http.StatusOK {
		t.Fatalf("health: %d %s", health.Code, health.Body.String())
	}
	if !strings.Contains(health.Body.String(), `"status":"ok"`) {
		t.Fatalf("health body: %s", health.Body.String())
	}

	unauthorized := httptest.NewRecorder()
	body, _ := json.Marshal(testEnvelope(dest("whend_http", "https://http.example/hook")))
	req := httptest.NewRequest(http.MethodPost, "/v1/dispatch", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	handler.ServeHTTP(unauthorized, req)
	if unauthorized.Code != http.StatusUnauthorized {
		t.Fatalf("want 401 without token, got %d", unauthorized.Code)
	}

	ok := httptest.NewRecorder()
	req2 := httptest.NewRequest(http.MethodPost, "/v1/dispatch", bytes.NewReader(body))
	req2.Header.Set("Content-Type", "application/json")
	req2.Header.Set("X-Boss-Dispatcher-Token", "disp-token")
	handler.ServeHTTP(ok, req2)
	if ok.Code != http.StatusOK {
		t.Fatalf("dispatch: %d %s", ok.Code, ok.Body.String())
	}
	var parsed Result
	if err := json.Unmarshal(ok.Body.Bytes(), &parsed); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(parsed.Receipts) != 1 || parsed.Receipts[0].Status != StatusSucceeded {
		t.Fatalf("handler result: %+v", parsed)
	}
}

func TestSignHelper(t *testing.T) {
	got := Sign([]byte("secret"), []byte(`{"ok":true}`))
	mac := hmac.New(sha256.New, []byte("secret"))
	mac.Write([]byte(`{"ok":true}`))
	want := "sha256=" + hex.EncodeToString(mac.Sum(nil))
	if got != want {
		t.Fatalf("Sign=%s want=%s", got, want)
	}
}

func TestPosterRecordsExactHeaders(t *testing.T) {
	var headers map[string]string
	poster := PosterFunc(func(url string, body []byte, h map[string]string) (int, string, error) {
		headers = h
		return 201, "", nil
	})
	d := newDispatcher(t, poster, &bytes.Buffer{}, false, 1)
	if _, err := d.Dispatch(testEnvelope(dest("whend_hdr", "https://hdr.example/hook"))); err != nil {
		t.Fatal(err)
	}
	if headers["Content-Type"] != "application/json" {
		t.Fatalf("content-type: %v", headers)
	}
	if headers["X-Boss-Event"] != "code_task.completed" {
		t.Fatalf("event header: %v", headers)
	}
	if headers["X-Boss-Delivery"] == "" {
		t.Fatal("missing X-Boss-Delivery")
	}
	if !strings.HasPrefix(headers["X-Hub-Signature-256"], "sha256=") {
		t.Fatalf("signature header: %v", headers)
	}
	_ = fmt.Sprintf("%v", headers) // keep import used if headers asserted
}
