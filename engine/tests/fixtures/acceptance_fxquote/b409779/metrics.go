package fxquote

import (
	"fmt"
	"sort"
	"strings"
	"sync"
)

var histBuckets = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10}

type reqKey struct {
	method string
	route  string
	status int
}

type histKey struct {
	method string
	route  string
}

type histogram struct {
	bucketCounts []int64
	sum          float64
	count        int64
}

type metricsRegistry struct {
	mu             sync.Mutex
	requestsTotal  map[reqKey]int64
	durations      map[histKey]*histogram
	quotesIssued   int64
}

func newMetricsRegistry() *metricsRegistry {
	return &metricsRegistry{
		requestsTotal: make(map[reqKey]int64),
		durations:     make(map[histKey]*histogram),
	}
}

func (m *metricsRegistry) recordRequest(method, route string, status int, durationSeconds float64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.requestsTotal[reqKey{method: method, route: route, status: status}]++

	hk := histKey{method: method, route: route}
	h, ok := m.durations[hk]
	if !ok {
		h = &histogram{bucketCounts: make([]int64, len(histBuckets))}
		m.durations[hk] = h
	}
	for i, b := range histBuckets {
		if durationSeconds <= b {
			h.bucketCounts[i]++
		}
	}
	h.sum += durationSeconds
	h.count++
}

func (m *metricsRegistry) incQuotesIssued() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.quotesIssued++
}

func escapeLabel(v string) string {
	v = strings.ReplaceAll(v, `\`, `\\`)
	v = strings.ReplaceAll(v, `"`, `\"`)
	v = strings.ReplaceAll(v, "\n", `\n`)
	return v
}

func (m *metricsRegistry) render(uptimeSeconds float64) string {
	m.mu.Lock()
	defer m.mu.Unlock()

	var b strings.Builder

	b.WriteString("# HELP http_requests_total Total HTTP requests\n")
	b.WriteString("# TYPE http_requests_total counter\n")
	reqKeys := make([]reqKey, 0, len(m.requestsTotal))
	for k := range m.requestsTotal {
		reqKeys = append(reqKeys, k)
	}
	sort.Slice(reqKeys, func(i, j int) bool {
		if reqKeys[i].route != reqKeys[j].route {
			return reqKeys[i].route < reqKeys[j].route
		}
		if reqKeys[i].method != reqKeys[j].method {
			return reqKeys[i].method < reqKeys[j].method
		}
		return reqKeys[i].status < reqKeys[j].status
	})
	for _, k := range reqKeys {
		fmt.Fprintf(&b, "http_requests_total{method=\"%s\",route=\"%s\",status=\"%d\"} %d\n",
			escapeLabel(k.method), escapeLabel(k.route), k.status, m.requestsTotal[k])
	}

	b.WriteString("# HELP http_request_duration_seconds HTTP request latency\n")
	b.WriteString("# TYPE http_request_duration_seconds histogram\n")
	histKeys := make([]histKey, 0, len(m.durations))
	for k := range m.durations {
		histKeys = append(histKeys, k)
	}
	sort.Slice(histKeys, func(i, j int) bool {
		if histKeys[i].route != histKeys[j].route {
			return histKeys[i].route < histKeys[j].route
		}
		return histKeys[i].method < histKeys[j].method
	})
	for _, k := range histKeys {
		h := m.durations[k]
		for i, boundary := range histBuckets {
			fmt.Fprintf(&b, "http_request_duration_seconds_bucket{method=\"%s\",route=\"%s\",le=\"%g\"} %d\n",
				escapeLabel(k.method), escapeLabel(k.route), boundary, h.bucketCounts[i])
		}
		fmt.Fprintf(&b, "http_request_duration_seconds_bucket{method=\"%s\",route=\"%s\",le=\"+Inf\"} %d\n",
			escapeLabel(k.method), escapeLabel(k.route), h.count)
		fmt.Fprintf(&b, "http_request_duration_seconds_sum{method=\"%s\",route=\"%s\"} %g\n",
			escapeLabel(k.method), escapeLabel(k.route), h.sum)
		fmt.Fprintf(&b, "http_request_duration_seconds_count{method=\"%s\",route=\"%s\"} %d\n",
			escapeLabel(k.method), escapeLabel(k.route), h.count)
	}

	b.WriteString("# HELP app_info Build information\n")
	b.WriteString("# TYPE app_info gauge\n")
	fmt.Fprintf(&b, "app_info{version=\"%s\"} 1\n", "1.0.0")

	b.WriteString("# HELP app_uptime_seconds Seconds since process start\n")
	b.WriteString("# TYPE app_uptime_seconds gauge\n")
	fmt.Fprintf(&b, "app_uptime_seconds %g\n", uptimeSeconds)

	b.WriteString("# HELP quotes_issued_total Total FX quotes issued\n")
	b.WriteString("# TYPE quotes_issued_total counter\n")
	fmt.Fprintf(&b, "quotes_issued_total %d\n", m.quotesIssued)

	return b.String()
}
