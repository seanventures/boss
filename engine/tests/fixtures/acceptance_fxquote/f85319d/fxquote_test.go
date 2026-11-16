package fxquote

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// Expected implementation surface (to be built by the implementer):
//
//   type Server struct{ ... }
//   func NewServer(cfgPath string, quoteTTL time.Duration) (*Server, error)
//   func (s *Server) Handler() http.Handler
//
// Config YAML shape:
//
//   pairs:
//     USD/EUR: { rate: 0.91, spread_bps: 50 }
//     USD/GBP: { rate: 0.78, spread_bps: 50 }
//
// Quote response JSON:
//   { "id": "...", "from": "USD", "to": "EUR", "amount_in": 10000,
//     "amount_out": 9123, "rate": "0.9123", "expires_at": "..." }
//
// Error response JSON:
//   { "reason": "unknown_currency" | "non_positive_amount" | "no_usd_rate" }

func writeConfig(t *testing.T, yaml string) string {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, "rates.yaml")
	if err := os.WriteFile(p, []byte(yaml), 0o644); err != nil {
		t.Fatalf("write config: %v", err)
	}
	return p
}

const validConfig = `
pairs:
  USD/EUR:
    rate: 0.91
    spread_bps: 50
  USD/GBP:
    rate: 0.78
    spread_bps: 50
`

func newTestServer(t *testing.T, yaml string, ttl time.Duration) *Server {
	t.Helper()
	cfgPath := writeConfig(t, yaml)
	srv, err := NewServer(cfgPath, ttl)
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	return srv
}

type quoteResp struct {
	ID        string `json:"id"`
	From      string `json:"from"`
	To        string `json:"to"`
	AmountIn  int64  `json:"amount_in"`
	AmountOut int64  `json:"amount_out"`
	Rate      string `json:"rate"`
	ExpiresAt string `json:"expires_at"`
}

type errResp struct {
	Reason string `json:"reason"`
}

func doQuote(t *testing.T, h http.Handler, query string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/quote?"+query, nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

// P0: the app starts — config loads and the handler serves a basic request.
func TestP0_ServerStartsAndServesQuote(t *testing.T) {
	srv := newTestServer(t, validConfig, 30*time.Second)
	rec := doQuote(t, srv.Handler(), "from=USD&to=EUR&amount=10000")
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
}

// P1 (criterion 1): spread applied and half-even rounding to minor units.
func TestP1_SpreadAppliedAndRoundedHalfEven(t *testing.T) {
	srv := newTestServer(t, validConfig, 30*time.Second)
	rec := doQuote(t, srv.Handler(), "from=USD&to=EUR&amount=10000")
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	var got quoteResp
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode: %v", err)
	}
	// mid-rate 0.91 with 50bps spread applied against the counterparty ->
	// effective rate 0.91 * (1 - 0.005) = 0.90545; 10000 * 0.90545 = 9054.5
	// half-even rounds .5 to the nearest even integer minor unit -> 9054.
	if got.AmountOut != 9054 {
		t.Fatalf("expected amount_out 9054 (half-even), got %d", got.AmountOut)
	}
	if got.AmountIn != 10000 || got.From != "USD" || got.To != "EUR" {
		t.Fatalf("unexpected echo fields: %+v", got)
	}
}

// P1 (criterion 2): a pair missing from config is derived via USD.
func TestP1_CrossRateDerivedViaUSD(t *testing.T) {
	srv := newTestServer(t, validConfig, 30*time.Second)
	rec := doQuote(t, srv.Handler(), "from=EUR&to=GBP&amount=10000")
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200 for derived cross rate, got %d: %s", rec.Code, rec.Body.String())
	}
}

// P1 (criterion 2): a currency with no USD rate returns 400 no_usd_rate.
func TestP1_NoUSDRateReturns400(t *testing.T) {
	cfg := `
pairs:
  USD/EUR:
    rate: 0.91
    spread_bps: 50
  EUR/JPY:
    rate: 160.0
    spread_bps: 50
`
	srv := newTestServer(t, cfg, 30*time.Second)
	rec := doQuote(t, srv.Handler(), "from=USD&to=JPY&amount=10000")
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", rec.Code, rec.Body.String())
	}
	var got errResp
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if got.Reason != "no_usd_rate" {
		t.Fatalf("expected reason no_usd_rate, got %q", got.Reason)
	}
}

// P1 (criterion 3): unknown currency rejected with fixed reason before any math.
func TestP1_UnknownCurrencyReturns400(t *testing.T) {
	srv := newTestServer(t, validConfig, 30*time.Second)
	rec := doQuote(t, srv.Handler(), "from=USD&to=ZZZ&amount=10000")
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", rec.Code, rec.Body.String())
	}
	var got errResp
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if got.Reason != "unknown_currency" {
		t.Fatalf("expected reason unknown_currency, got %q", got.Reason)
	}
}

// P1 (criterion 3): non-positive / malformed amount rejected, including boundary and -0.
func TestP1_NonPositiveAmountReturns400(t *testing.T) {
	srv := newTestServer(t, validConfig, 30*time.Second)
	cases := []string{
		"from=USD&to=EUR&amount=0",
		"from=USD&to=EUR&amount=-1",
		"from=USD&to=EUR&amount=-0",
		"from=USD&to=EUR&amount=notanumber",
	}
	for _, q := range cases {
		rec := doQuote(t, srv.Handler(), q)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("query %q: expected 400, got %d: %s", q, rec.Code, rec.Body.String())
		}
		var got errResp
		if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
			t.Fatalf("query %q: decode: %v", q, err)
		}
		if got.Reason == "" {
			t.Fatalf("query %q: expected non-empty reason", q)
		}
	}
}

// P1 (criterion 4): unknown/never-issued quote id returns 404.
func TestP1_UnknownQuoteIDReturns404(t *testing.T) {
	srv := newTestServer(t, validConfig, 30*time.Second)
	req := httptest.NewRequest(http.MethodGet, "/quote/never-issued-id", nil)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d: %s", rec.Code, rec.Body.String())
	}
}

// P1 (criterion 4): an expired quote id returns 410.
func TestP1_ExpiredQuoteIDReturns410(t *testing.T) {
	srv := newTestServer(t, validConfig, 10*time.Millisecond)
	rec := doQuote(t, srv.Handler(), "from=USD&to=EUR&amount=10000")
	if rec.Code != http.StatusOK {
		t.Fatalf("setup quote failed: %d %s", rec.Code, rec.Body.String())
	}
	var q quoteResp
	if err := json.Unmarshal(rec.Body.Bytes(), &q); err != nil {
		t.Fatalf("decode: %v", err)
	}
	time.Sleep(50 * time.Millisecond)
	req := httptest.NewRequest(http.MethodGet, "/quote/"+q.ID, nil)
	rec2 := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec2, req)
	if rec2.Code != http.StatusGone {
		t.Fatalf("expected 410, got %d: %s", rec2.Code, rec2.Body.String())
	}
}

// P1 (criterion 5): quote ids are non-empty and non-colliding across many issuances.
func TestP1_QuoteIDsAreUniqueAtScale(t *testing.T) {
	srv := newTestServer(t, validConfig, 30*time.Second)
	seen := make(map[string]bool, 10000)
	for i := 0; i < 10000; i++ {
		rec := doQuote(t, srv.Handler(), "from=USD&to=EUR&amount=10000")
		if rec.Code != http.StatusOK {
			t.Fatalf("iteration %d: expected 200, got %d", i, rec.Code)
		}
		var q quoteResp
		if err := json.Unmarshal(rec.Body.Bytes(), &q); err != nil {
			t.Fatalf("iteration %d: decode: %v", i, err)
		}
		if q.ID == "" {
			t.Fatalf("iteration %d: empty quote id", i)
		}
		if seen[q.ID] {
			t.Fatalf("iteration %d: duplicate quote id %q", i, q.ID)
		}
		seen[q.ID] = true
	}
}

// P1 (criterion 6): readiness fails loudly at startup on bad config (no USD rate for a currency).
func TestP1_ReadyzFailsOnBadConfig(t *testing.T) {
	badCfg := `
pairs:
  EUR/GBP:
    rate: 0.86
    spread_bps: 50
`
	cfgPath := writeConfig(t, badCfg)
	_, err := NewServer(cfgPath, 30*time.Second)
	if err == nil {
		t.Fatalf("expected NewServer to fail on config with no USD-reachable currency")
	}
}

// P1 (criterion 6): healthz/readyz respond 200 on valid config.
func TestP1_HealthzAndReadyzOK(t *testing.T) {
	srv := newTestServer(t, validConfig, 30*time.Second)
	for _, path := range []string{"/healthz", "/readyz"} {
		req := httptest.NewRequest(http.MethodGet, path, nil)
		rec := httptest.NewRecorder()
		srv.Handler().ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("%s: expected 200, got %d", path, rec.Code)
		}
	}
}

// P1 (criterion 9): error responses never leak internal state (paths, error chains, raw rates).
func TestP1_ErrorResponsesDoNotLeakInternals(t *testing.T) {
	srv := newTestServer(t, validConfig, 30*time.Second)
	rec := doQuote(t, srv.Handler(), "from=USD&to=ZZZ&amount=10000")
	body := rec.Body.String()
	for _, forbidden := range []string{".yaml", "/tmp", "panic", "goroutine", "0.91", "0.78"} {
		if strings.Contains(body, forbidden) {
			t.Fatalf("error body leaks internal detail %q: %s", forbidden, body)
		}
	}
}

// --- Metrics (BOSS platform standard) ---

// P1: /metrics answers 200 with the Prometheus text exposition content-type.
func TestP1_MetricsEndpointServesPrometheusFormat(t *testing.T) {
	srv := newTestServer(t, validConfig, 30*time.Second)
	req := httptest.NewRequest(http.MethodGet, "/metrics", nil)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	ct := rec.Header().Get("Content-Type")
	if !strings.HasPrefix(ct, "text/plain; version=0.0.4") {
		t.Fatalf("expected text/plain; version=0.0.4 content-type, got %q", ct)
	}
	body := rec.Body.String()
	for _, series := range []string{
		"http_requests_total",
		"http_request_duration_seconds",
		"app_info",
		"app_uptime_seconds",
	} {
		if !strings.Contains(body, series) {
			t.Fatalf("missing required series %q in metrics output", series)
		}
	}
}

// P1: issuing a quote increments a domain counter that proves the quote feature is working.
func TestP1_QuoteMetricIncrementsOnIssue(t *testing.T) {
	srv := newTestServer(t, validConfig, 30*time.Second)
	h := srv.Handler()

	before := scrapeBody(t, h)
	rec := doQuote(t, h, "from=USD&to=EUR&amount=10000")
	if rec.Code != http.StatusOK {
		t.Fatalf("setup quote failed: %d", rec.Code)
	}
	after := scrapeBody(t, h)

	if !metricIncreased(before, after, "quotes_issued_total") {
		t.Fatalf("expected a quotes_issued_total-style counter to increase after issuing a quote")
	}
}

// P1: route labels on http_requests_total are templates, not raw paths with IDs embedded.
func TestP1_MetricsRouteLabelsAreTemplated(t *testing.T) {
	srv := newTestServer(t, validConfig, 30*time.Second)
	h := srv.Handler()

	rec := doQuote(t, h, "from=USD&to=EUR&amount=10000")
	var q quoteResp
	if err := json.Unmarshal(rec.Body.Bytes(), &q); err != nil {
		t.Fatalf("decode: %v", err)
	}

	req1 := httptest.NewRequest(http.MethodGet, "/quote/"+q.ID, nil)
	srv.Handler().ServeHTTP(httptest.NewRecorder(), req1)
	req2 := httptest.NewRequest(http.MethodGet, "/quote/some-other-id", nil)
	srv.Handler().ServeHTTP(httptest.NewRecorder(), req2)

	body := scrapeBody(t, h)
	if strings.Contains(body, q.ID) || strings.Contains(body, "some-other-id") {
		t.Fatalf("metrics output embeds a raw quote id as a label value, must use a route template:\n%s", body)
	}
}

// P1: scraping /metrics does not itself appear in the app's own request counters.
func TestP1_MetricsScrapeExcludedFromOwnCounters(t *testing.T) {
	srv := newTestServer(t, validConfig, 30*time.Second)
	h := srv.Handler()

	before := scrapeBody(t, h)
	after := scrapeBody(t, h) // this scrape itself must not count
	beforeCount := countOccurrences(before, "/metrics")
	afterCount := countOccurrences(after, "/metrics")
	if afterCount > beforeCount {
		t.Fatalf("scraping /metrics increased its own route's request count (before=%d after=%d)", beforeCount, afterCount)
	}
}

func scrapeBody(t *testing.T, h http.Handler) string {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/metrics", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("scrape /metrics: expected 200, got %d", rec.Code)
	}
	return rec.Body.String()
}

func countOccurrences(s, substr string) int {
	return strings.Count(s, substr)
}

func metricIncreased(before, after, namePrefix string) bool {
	beforeVal := sumMetricValues(before, namePrefix)
	afterVal := sumMetricValues(after, namePrefix)
	return afterVal > beforeVal
}

// sumMetricValues sums the numeric values of all exposition lines whose metric name
// starts with namePrefix (ignoring labels), tolerant of the counter not existing yet.
func sumMetricValues(body, namePrefix string) float64 {
	var total float64
	for _, line := range strings.Split(body, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if !strings.HasPrefix(line, namePrefix) {
			continue
		}
		parts := strings.Fields(line)
		if len(parts) != 2 {
			continue
		}
		var v float64
		if _, err := fmt.Sscan(parts[1], &v); err == nil {
			total += v
		}
	}
	return total
}
