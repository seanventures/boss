package lbd

import (
	"fmt"
	"io"
	"sort"
	"sync"
	"time"
)

// Metrics is a tiny Prometheus text exporter: counters, gauges and one
// histogram per name. No dependency; GET /metrics on the loopback port.
type Metrics struct {
	mu       sync.Mutex
	counters map[string]float64
	gauges   map[string]float64
	hists    map[string]*histogram
}

var withdrawBuckets = []float64{0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1, 2}

type histogram struct {
	buckets []float64
	counts  []uint64
	sum     float64
	n       uint64
	last    float64
	max     float64
}

func NewMetrics() *Metrics {
	return &Metrics{counters: map[string]float64{}, gauges: map[string]float64{}, hists: map[string]*histogram{}}
}

func (m *Metrics) Inc(name string, by float64) {
	m.mu.Lock()
	m.counters[name] += by
	m.mu.Unlock()
}

func (m *Metrics) Set(name string, v float64) {
	m.mu.Lock()
	m.gauges[name] = v
	m.mu.Unlock()
}

// Observe records a duration into name's histogram (seconds).
func (m *Metrics) Observe(name string, d time.Duration) {
	m.mu.Lock()
	defer m.mu.Unlock()
	h := m.hists[name]
	if h == nil {
		h = &histogram{buckets: withdrawBuckets, counts: make([]uint64, len(withdrawBuckets))}
		m.hists[name] = h
	}
	s := d.Seconds()
	for i, b := range h.buckets {
		if s <= b {
			h.counts[i]++
		}
	}
	h.sum += s
	h.n++
	h.last = s
	if s > h.max {
		h.max = s
	}
}

// Last is the most recent observation of a histogram (tests, logs).
func (m *Metrics) Last(name string) (time.Duration, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	h := m.hists[name]
	if h == nil || h.n == 0 {
		return 0, false
	}
	return time.Duration(h.last * float64(time.Second)), true
}

// Write renders the exposition format.
func (m *Metrics) Write(w io.Writer) {
	m.mu.Lock()
	defer m.mu.Unlock()
	names := func(mm map[string]float64) []string {
		out := make([]string, 0, len(mm))
		for k := range mm {
			out = append(out, k)
		}
		sort.Strings(out)
		return out
	}
	for _, k := range names(m.counters) {
		fmt.Fprintf(w, "# TYPE %s counter\n%s %g\n", k, k, m.counters[k])
	}
	for _, k := range names(m.gauges) {
		fmt.Fprintf(w, "# TYPE %s gauge\n%s %g\n", k, k, m.gauges[k])
	}
	hk := make([]string, 0, len(m.hists))
	for k := range m.hists {
		hk = append(hk, k)
	}
	sort.Strings(hk)
	for _, k := range hk {
		h := m.hists[k]
		fmt.Fprintf(w, "# TYPE %s histogram\n", k)
		for i, b := range h.buckets {
			fmt.Fprintf(w, "%s_bucket{le=\"%g\"} %d\n", k, b, h.counts[i])
		}
		fmt.Fprintf(w, "%s_bucket{le=\"+Inf\"} %d\n%s_sum %g\n%s_count %d\n%s_max %g\n", k, h.n, k, h.sum, k, h.n, k, h.max)
	}
}
