package fxquote

import (
	"crypto/rand"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"gopkg.in/yaml.v3"
)

//go:embed web/index.html
var indexHTML []byte

// --- config ---

type pairConfig struct {
	Rate      float64 `yaml:"rate"`
	SpreadBps float64 `yaml:"spread_bps"`
}

type rawConfig struct {
	Pairs map[string]pairConfig `yaml:"pairs"`
}

type rateTable struct {
	pairs      map[string]pairConfig // key: "FROM/TO"
	currencies map[string]bool
}

func loadRateTable(cfgPath string) (*rateTable, error) {
	data, err := os.ReadFile(cfgPath)
	if err != nil {
		return nil, fmt.Errorf("read config: %w", err)
	}
	var raw rawConfig
	if err := yaml.Unmarshal(data, &raw); err != nil {
		return nil, fmt.Errorf("parse config: %w", err)
	}
	if len(raw.Pairs) == 0 {
		return nil, fmt.Errorf("config has no pairs")
	}

	rt := &rateTable{
		pairs:      make(map[string]pairConfig, len(raw.Pairs)),
		currencies: make(map[string]bool),
	}
	usdSeen := false
	for key, pc := range raw.Pairs {
		parts := strings.SplitN(key, "/", 2)
		if len(parts) != 2 {
			return nil, fmt.Errorf("invalid pair key %q", key)
		}
		if pc.Rate <= 0 {
			return nil, fmt.Errorf("invalid rate for pair %q", key)
		}
		if pc.SpreadBps < 0 || pc.SpreadBps >= 10000 {
			return nil, fmt.Errorf("invalid spread for pair %q", key)
		}
		rt.pairs[key] = pc
		rt.currencies[parts[0]] = true
		rt.currencies[parts[1]] = true
		if parts[0] == "USD" || parts[1] == "USD" {
			usdSeen = true
		}
	}
	if !usdSeen {
		return nil, fmt.Errorf("config has no USD-reachable currency pair")
	}
	return rt, nil
}

func (rt *rateTable) isKnown(cur string) bool {
	return rt.currencies[cur]
}

// usdRateOf returns how many units of cur equal 1 USD.
func (rt *rateTable) usdRateOf(cur string) (float64, bool) {
	if cur == "USD" {
		return 1, true
	}
	if pc, ok := rt.pairs["USD/"+cur]; ok {
		return pc.Rate, true
	}
	if pc, ok := rt.pairs[cur+"/USD"]; ok {
		return 1 / pc.Rate, true
	}
	return 0, false
}

// resolve returns the mid rate (1 from = rate to) and spread in bps, or an error reason.
func (rt *rateTable) resolve(from, to string) (rate float64, spreadBps float64, reason string) {
	if pc, ok := rt.pairs[from+"/"+to]; ok {
		return pc.Rate, pc.SpreadBps, ""
	}
	if pc, ok := rt.pairs[to+"/"+from]; ok {
		return 1 / pc.Rate, pc.SpreadBps, ""
	}
	usdFrom, ok1 := rt.usdRateOf(from)
	usdTo, ok2 := rt.usdRateOf(to)
	if !ok1 || !ok2 {
		return 0, 0, "no_usd_rate"
	}
	spreadFrom, _ := rt.legSpread(from)
	spreadTo, _ := rt.legSpread(to)
	return usdTo / usdFrom, (spreadFrom + spreadTo) / 2, ""
}

func (rt *rateTable) legSpread(cur string) (float64, bool) {
	if cur == "USD" {
		return 0, true
	}
	if pc, ok := rt.pairs["USD/"+cur]; ok {
		return pc.SpreadBps, true
	}
	if pc, ok := rt.pairs[cur+"/USD"]; ok {
		return pc.SpreadBps, true
	}
	return 0, false
}

// --- quote store ---

type storedQuote struct {
	resp      quoteResponse
	expiresAt time.Time
}

type quoteStore struct {
	mu   sync.Mutex
	data map[string]storedQuote
}

func newQuoteStore() *quoteStore {
	return &quoteStore{data: make(map[string]storedQuote)}
}

func (qs *quoteStore) put(id string, resp quoteResponse, expiresAt time.Time) {
	qs.mu.Lock()
	defer qs.mu.Unlock()
	qs.data[id] = storedQuote{resp: resp, expiresAt: expiresAt}
}

func (qs *quoteStore) get(id string) (storedQuote, bool) {
	qs.mu.Lock()
	defer qs.mu.Unlock()
	sq, ok := qs.data[id]
	return sq, ok
}

func newQuoteID() (string, error) {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return "q_" + hex.EncodeToString(buf), nil
}

// --- API types ---

type quoteResponse struct {
	ID        string `json:"id"`
	From      string `json:"from"`
	To        string `json:"to"`
	AmountIn  int64  `json:"amount_in"`
	AmountOut int64  `json:"amount_out"`
	Rate      string `json:"rate"`
	ExpiresAt string `json:"expires_at"`
}

type errorResponse struct {
	Reason string `json:"reason"`
}

// --- server ---

type Server struct {
	rates     *rateTable
	quotes    *quoteStore
	ttl       time.Duration
	mux       *http.ServeMux
	metrics   *metricsRegistry
	startedAt time.Time
}

func NewServer(cfgPath string, quoteTTL time.Duration) (*Server, error) {
	rt, err := loadRateTable(cfgPath)
	if err != nil {
		return nil, err
	}

	s := &Server{
		rates:     rt,
		quotes:    newQuoteStore(),
		ttl:       quoteTTL,
		metrics:   newMetricsRegistry(),
		startedAt: time.Now(),
	}
	s.mux = http.NewServeMux()
	s.mux.HandleFunc("GET /", s.handleIndex)
	s.mux.HandleFunc("GET /quote", s.handleQuote)
	s.mux.HandleFunc("GET /quote/{id}", s.handleQuoteByID)
	s.mux.HandleFunc("GET /healthz", s.handleHealthz)
	s.mux.HandleFunc("GET /readyz", s.handleReadyz)
	s.mux.HandleFunc("GET /metrics", s.handleMetrics)
	return s, nil
}

func (s *Server) Handler() http.Handler {
	return s.instrument(s.mux)
}

func (s *Server) instrument(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/metrics" {
			h.ServeHTTP(w, r)
			return
		}
		_, pattern := s.mux.Handler(r)
		route := routeTemplate(pattern)
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		start := time.Now()
		h.ServeHTTP(rec, r)
		dur := time.Since(start).Seconds()
		s.metrics.recordRequest(r.Method, route, rec.status, dur)
	})
}

func routeTemplate(pattern string) string {
	if pattern == "" {
		return "unmatched"
	}
	parts := strings.SplitN(pattern, " ", 2)
	if len(parts) == 2 {
		return parts[1]
	}
	return pattern
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

func writeError(w http.ResponseWriter, status int, reason string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(errorResponse{Reason: reason})
}

func (s *Server) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(indexHTML)
}

func (s *Server) handleQuote(w http.ResponseWriter, r *http.Request) {
	from := r.URL.Query().Get("from")
	to := r.URL.Query().Get("to")
	amountStr := r.URL.Query().Get("amount")

	if !s.rates.isKnown(from) || !s.rates.isKnown(to) {
		s.metrics.incQuotesRejected("unknown_currency")
		writeError(w, http.StatusBadRequest, "unknown_currency")
		return
	}

	amountIn, err := strconv.ParseInt(amountStr, 10, 64)
	if err != nil {
		s.metrics.incQuotesRejected("malformed_amount")
		writeError(w, http.StatusBadRequest, "malformed_amount")
		return
	}
	if amountIn <= 0 {
		s.metrics.incQuotesRejected("non_positive_amount")
		writeError(w, http.StatusBadRequest, "non_positive_amount")
		return
	}

	rate, spreadBps, reason := s.rates.resolve(from, to)
	if reason != "" {
		s.metrics.incQuotesRejected(reason)
		writeError(w, http.StatusBadRequest, reason)
		return
	}

	effectiveRate := rate * (1 - spreadBps/10000)
	amountOut := int64(math.RoundToEven(float64(amountIn) * effectiveRate))

	id, err := newQuoteID()
	if err != nil {
		s.metrics.incQuotesRejected("internal_error")
		writeError(w, http.StatusBadRequest, "internal_error")
		return
	}

	now := time.Now()
	resp := quoteResponse{
		ID:        id,
		From:      from,
		To:        to,
		AmountIn:  amountIn,
		AmountOut: amountOut,
		Rate:      strconv.FormatFloat(effectiveRate, 'f', 4, 64),
		ExpiresAt: now.Add(s.ttl).UTC().Format(time.RFC3339),
	}
	s.quotes.put(id, resp, now.Add(s.ttl))
	s.metrics.incQuotesIssued()

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(resp)
}

func (s *Server) handleQuoteByID(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	sq, ok := s.quotes.get(id)
	if !ok {
		w.WriteHeader(http.StatusNotFound)
		return
	}
	if time.Now().After(sq.expiresAt) {
		w.WriteHeader(http.StatusGone)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(sq.resp)
}

func (s *Server) handleHealthz(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
}

func (s *Server) handleReadyz(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
}

func (s *Server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(s.metrics.render(time.Since(s.startedAt).Seconds())))
}
