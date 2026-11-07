/**
 * Observability fixtures: WIRE bodies (snake_case, exactly engine/boss_engine/services/obs_view.py's
 * TypedDicts) for every Observability view, the routes the tabs call beside them, and
 * `mockObsFetch()`, which routes `fetch(url)` to them and records every write.
 *
 * The ids are the parity table's (lib/infra-obs-parity.ts header): targets `t_live` (Up, on
 * `h_ready` boss-1), `t_down` (Down, on `h_down` edge-2), `t_cloud` (registry, not placed, Not
 * deployed); deploys `stack:sd_live`, `stack:sd_failed`. Shared with the lead's parity test.
 *
 * Every number here is chosen so a test can recount it by hand (the strip helpers below say
 * exactly which cells are gaps, amber or red).
 */
import { vi } from "vitest";

import { board } from "./setup-fixtures";
import { INCIDENT_REPORT_WIRE, incidentResponseWire } from "./incident-fixtures";
import { allAlertsWire, historyWire, rulesWire } from "./alerts-fixtures";

export const NOW = "2026-10-04T12:00:00.000Z";
export const NOW_MS = Date.parse(NOW);
const H = 3600_000;
const iso = (ms: number) => new Date(ms).toISOString();
export const FROM_24H = iso(NOW_MS - 24 * H);

// ── the Pulse ───────────────────────────────────────────────────────────────────────────────

export type WireCell = [number, number, number | null];

/** 96 cells of 8 ok probes, p95 `p95`, with `patch` overriding cell indices. */
export function strip(p95: number, patch: Record<number, WireCell> = {}, cells = 96): WireCell[] {
  return Array.from({ length: cells }, (_, i) => patch[i] ?? [8, 8, p95 + (i % 5) * 7]);
}

/** t_live: up, two gap cells (40, 41), one amber cell (60: 7 of 8). */
export const LIVE_STRIP = strip(212, { 40: [0, 0, null], 41: [0, 0, null], 60: [7, 8, 412] });
/** t_down: fine until cell 91, then every probe failed (red, hatched). */
export const DOWN_STRIP = strip(240, { 91: [0, 8, null], 92: [0, 8, null], 93: [0, 8, null], 94: [0, 8, null], 95: [0, 8, null] });
const SLOW_STRIP = strip(2400, { 70: [6, 8, 3100] });
const QUIET_STRIP = strip(0, Object.fromEntries(Array.from({ length: 96 }, (_, i) => [i, [0, 0, null] as WireCell])));

export const PIN_LIVE = { at: iso(NOW_MS - 10 * H + 12 * 60_000), sha7: "a1b2c3d", word: "Live", tone: "green", ref: "stack:sd_live" };
export const PIN_FAILED = { at: iso(NOW_MS - 70 * 60_000), sha7: "e4f5a6b", word: "Failed", tone: "red", ref: "stack:sd_failed" };

export const ATTENTION_APP_DOWN = {
  id: "infra:app_down:t_down", kind: "infra", infra_kind: "app_down", title: "api is down",
  detail: "No probe answered for 64m", role_key: null, actions: [], answers: [],
  decide_inline: false, at: iso(NOW_MS - 64 * 60_000),
  open: "?area=observability&view=apps&app=t_down", area: "observability",
};
export const ATTENTION_SYSTEM = {
  id: "system:f_mem", kind: "system_health", finding_id: "f_mem", finding_kind: "memory_high",
  severity: "warn", title: "Memory high for 40m", detail: "Above 75% since 11:20. 3 runs active.",
  what_to_do: null, role_key: null,
  answers: [
    { key: "free_caches", label: "Free caches", enabled: true, reason: "Drops page cache. Nothing stops.", confirm: false },
    { key: "stop_idle_previews", label: "Stop idle previews", enabled: true, reason: "Stops previews idle over 30m.", confirm: true },
  ],
  actions: ["free_caches", "stop_idle_previews"], decide_inline: false, at: iso(NOW_MS - 40 * 60_000),
  area: "observability",
};
export const FINDING_NO_FIX = {
  id: "system:f_disk", kind: "system_health", finding_id: "f_disk", finding_kind: "disk_trend",
  severity: "info", title: "Disk filling slowly", detail: "Up 3% this week.", what_to_do: null, role_key: null,
  answers: [], actions: [], decide_inline: false, at: iso(NOW_MS - 3 * H), area: "observability",
};

export function pulseRow(over: Record<string, unknown> & { target_id: string }): Record<string, unknown> & { target_id: string } {
  return { host: null, since: null, uptime: null, p95_ms: null, strip: [], pins: [], incidents: 0, ...over };
}

export const PULSE_ROWS = [
  pulseRow({ target_id: "t_down", app: "api", env: "prod", host: { id: "h_down", name: "edge-2" }, state: "down", word: "Down",
             tone: "red", since: iso(NOW_MS - 64 * 60_000), uptime: 0.913, p95_ms: 240, strip: DOWN_STRIP, pins: [PIN_FAILED], incidents: 1 }),
  pulseRow({ target_id: "t_slow", app: "team-inbox", env: "dev", host: { id: "h_ready", name: "boss-1" }, state: "degraded", word: "Slow",
             tone: "amber", since: iso(NOW_MS - 5 * H), uptime: 0.981, p95_ms: 2400, strip: SLOW_STRIP }),
  pulseRow({ target_id: "t_quiet", app: "invoice", env: "dev", host: { id: "h_ready", name: "boss-1" }, state: "unknown", word: "No signal",
             tone: "hollow", strip: QUIET_STRIP }),
  pulseRow({ target_id: "t_live", app: "invoice", env: "prod", host: { id: "h_ready", name: "boss-1" }, state: "up", word: "Up",
             tone: "green", since: iso(NOW_MS - 30 * H), uptime: 0.999, p95_ms: 212, strip: LIVE_STRIP, pins: [PIN_LIVE] }),
  pulseRow({ target_id: "t_cloud", app: "billing", env: "prod", state: "not_deployed", word: "Not deployed", tone: "grey" }),
];

/** Grafana as the server reports it (observability_stack.endpoint_state, recorded checks only). */
export const GRAFANA_UP = { state: "answering", url: "http://grafana.local", reason: null, checked_at: NOW, why: null };
export const GRAFANA_DOWN = { state: "not_answering", url: null, reason: "Grafana isn't answering on its host",
                              checked_at: NOW, why: "connection refused" };

export function pulseWire(over: Record<string, unknown> = {}) {
  return {
    counts: { up: 1, slow: 1, down: 1, no_signal: 1, not_deployed: 1 },
    rows: PULSE_ROWS,
    not_watched: [{ app: "orders", env: "dev", scrape_url: "http://10.0.0.4:9102/metrics", dashboard_url: "http://grafana.local/d/orders" }],
    window: { win: "24h", from: FROM_24H, to: NOW, cell_s: 900, cells: 96 },
    attention: [ATTENTION_APP_DOWN, ATTENTION_SYSTEM],
    spend_24h_micros: 3_120_000, spend_24h_estimated: true, partial: [], etag: "\"obs-pulse\"",
    grafana: GRAFANA_UP, alerts_firing: 2,
    ...over,
  };
}
export const emptyPulseWire = () => pulseWire({ counts: { up: 0, slow: 0, down: 0, no_signal: 0, not_deployed: 0 }, rows: [], not_watched: [], attention: [], spend_24h_micros: 0 });

// ── one app's drawer ────────────────────────────────────────────────────────────────────────

export function appWire(targetId = "t_live", over: Record<string, unknown> = {}) {
  const down = targetId === "t_down";
  const series = Array.from({ length: 96 }, (_, i) => {
    const bad = down ? i >= 91 : i === 60;
    const gap = !down && (i === 40 || i === 41);
    return { at: iso(NOW_MS - 24 * H + i * 900_000), p95: gap || (down && bad) ? null : 212 + (i % 7) * 11,
             avg: gap || (down && bad) ? null : 120 + (i % 5) * 6, ok: gap ? 0 : bad ? (down ? 0 : 7) : 8, n: gap ? 0 : 8 };
  });
  return {
    target_id: targetId, app: down ? "api" : "invoice", env: "prod",
    state: down ? "down" : "up", word: down ? "Down" : "Up", tone: down ? "red" : "green",
    since: iso(NOW_MS - (down ? 64 / 60 : 30) * H),
    facts: {
      uptime_24h: down ? 0.913 : 0.999, uptime_7d: down ? 0.987 : 0.9995, p95_ms: down ? 240 : 212, avg_ms: down ? 140 : 121,
      last_probe_at: iso(NOW_MS - 90_000), probes: 762, failed: down ? 40 : 3, build_sha7: "a1b2c3d", deployed_at: PIN_LIVE.at,
      observed_version: down ? "a1b2c3d" : "9f1e2d3", serving_mismatch: !down,
      host: down ? { id: "h_down", name: "edge-2" } : { id: "h_ready", name: "boss-1" },
      open_url: down ? "http://edge-2:8080" : "https://invoice.example.com", dashboard_url: down ? null : "http://grafana.local/d/invoice",
      dashboard_reason: down ? "Grafana isn't answering on its host" : null,
      grafana: down ? GRAFANA_DOWN : GRAFANA_UP,
      last_error: down ? "connect ECONNREFUSED 10.0.0.9:8080 — the upstream refused the connection after 3 retries" : null,
    },
    series,
    incidents: [
      { start: iso(NOW_MS - 64 * 60_000), end: null, duration_s: null,
        first_error: "connect ECONNREFUSED 10.0.0.9:8080 — the upstream refused the connection after 3 retries",
        deploy_before: { ref: "stack:sd_failed", sha7: "e4f5a6b" } },
      { start: iso(NOW_MS - 9 * H), end: iso(NOW_MS - 9 * H + 15 * 60_000), duration_s: 900,
        first_error: "HTTP 502", deploy_before: null },
    ],
    pins: down ? [PIN_FAILED] : [PIN_LIVE],
    window: { win: "24h", from: FROM_24H, to: NOW, cell_s: 900, cells: 96 },
    // t_down's own metrics carry an open error-rate incident, so its drawer draws the Incident card.
    ...(down ? { metrics: { declared: true, path: "/metrics", via: "auto", interval_s: 120, state: "ok", word: "Scraped",
      last_scrape_at: iso(NOW_MS - 20_000), requests: 220, errors: 27, error_rate: 0.125, rate_per_min: 44, p50_ms: 60, p95_ms: 440,
      window_minutes: 5, series: [],
      alert: { state: "firing", word: "Alerting", incident_id: "inc_1", incident_status: "open", since: iso(NOW_MS - 12 * 60_000),
               rule: { error_rate_percent: 5, window_minutes: 5, min_requests: 20, enabled: true } } } } : {}),
    etag: `"obs-app-${targetId}"`,
    ...over,
  };
}

// ── Models ──────────────────────────────────────────────────────────────────────────────────

const BUCKETS = Array.from({ length: 24 }, (_, i) => iso(NOW_MS - 24 * H + i * H));
const wave = (base: number, i: number) => Math.round(base * (0.6 + 0.4 * Math.sin(i / 3) ** 2));

export type WireGroup = Record<string, unknown> & { key: string; label: string };
export function groupRow(over: Record<string, unknown> & { key: string; label: string }): WireGroup {
  return { role_key: null, guest: false, calls: 0, tokens_in: 0, tokens_out: 0, spend_micros: 0, included: false,
           estimated: false, avg_ms: null, p95_ms: null, failed: 0, retries: 0,
           trend: Array.from({ length: 24 }, (_, i) => wave(10, i)), ...over };
}

const MODEL_GROUPS = [
  groupRow({ key: "claude-sonnet-4-5", label: "claude-sonnet-4-5", calls: 612, tokens_in: 2_900_000, tokens_out: 402_000, spend_micros: 2_410_000, estimated: true, avg_ms: 9200, p95_ms: 21000, failed: 7, retries: 14 }),
  groupRow({ key: "gpt-5-mini", label: "gpt-5-mini", calls: 401, tokens_in: 700_000, tokens_out: 120_000, spend_micros: 650_000, avg_ms: 3100, p95_ms: 7400, failed: 12, retries: 9 }),
  groupRow({ key: "claude-opus-4", label: "claude-opus-4", calls: 120, tokens_in: 300_000, tokens_out: 50_000, spend_micros: 0, included: true, avg_ms: 14000, p95_ms: 30000, failed: 2, retries: 1 }),
  groupRow({ key: "gemini-2.5-pro", label: "gemini-2.5-pro", calls: 80, tokens_in: 120_000, tokens_out: 22_000, spend_micros: 56_000, avg_ms: 6100, p95_ms: 12000, failed: 4, retries: 3 }),
  groupRow({ key: "qwen-local", label: "qwen-local", calls: 50, tokens_in: 60_000, tokens_out: 12_000, spend_micros: 0, included: false, avg_ms: 2200, p95_ms: 4100, failed: 1, retries: 0 }),
  groupRow({ key: "gpt-4o", label: "gpt-4o", calls: 21, tokens_in: 20_000, tokens_out: 4_000, spend_micros: 4_000, avg_ms: 2500, p95_ms: 5000, failed: 1, retries: 0 }),
];
const STAGE_GROUPS = [
  groupRow({ key: "implement", label: "implement", calls: 700, tokens_in: 3_000_000, tokens_out: 420_000, spend_micros: 2_600_000, estimated: true, avg_ms: 11000, p95_ms: 22000, failed: 15, retries: 18 }),
  groupRow({ key: "review", label: "review", calls: 584, tokens_in: 1_100_000, tokens_out: 190_000, spend_micros: 520_000, avg_ms: 4100, p95_ms: 9000, failed: 12, retries: 9 }),
];
const ROLE_GROUPS = [
  groupRow({ key: "software_engineer", label: "software_engineer", role_key: "software_engineer", calls: 700, tokens_in: 3_000_000, tokens_out: 420_000, spend_micros: 2_600_000, avg_ms: 11000, p95_ms: 22000, failed: 15, retries: 18 }),
  groupRow({ key: "qa", label: "qa", role_key: "qa", calls: 584, tokens_in: 1_100_000, tokens_out: 190_000, spend_micros: 520_000, avg_ms: 4100, p95_ms: 9000, failed: 12, retries: 9 }),
];
const ENGINE_GROUPS = [
  groupRow({ key: "claude_code", label: "claude_code", calls: 900, tokens_in: 3_300_000, tokens_out: 470_000, spend_micros: 0, included: true, avg_ms: 10000, p95_ms: 21000, failed: 10, retries: 15 }),
  groupRow({ key: "guest:codex", label: "codex", guest: true, calls: 384, tokens_in: 800_000, tokens_out: 140_000, spend_micros: 3_120_000, estimated: true, avg_ms: 4000, p95_ms: 9000, failed: 17, retries: 12 }),
];
export const GROUP_ROWS: Record<string, WireGroup[]> = {
  model: MODEL_GROUPS, stage: STAGE_GROUPS, role: ROLE_GROUPS, engine: ENGINE_GROUPS,
};

export const ROUTING_ROWS = Array.from({ length: 8 }, (_, i) => ({
  id: `mrd_${i + 1}`, at: iso(NOW_MS - (i + 1) * 20 * 60_000), provider: i % 3 === 2 ? "openai" : "anthropic",
  budget: i === 4 ? "orange" : "green", boundary: i % 2 ? "internal" : "public",
  outcome: i === 5 ? "denied" : "selected", model: i === 5 ? null : i % 3 === 2 ? "gpt-5-mini" : "claude-sonnet-4-5",
}));

export function modelsWire(group = "model", over: Record<string, unknown> = {}) {
  const groups = GROUP_ROWS[group] ?? MODEL_GROUPS;
  const top = groups.slice(0, 5);
  const bands = top.map((g, k) => ({ key: g.key, label: g.label, values: BUCKETS.map((_, i) => wave(60_000 / (k + 1), i + k)) }));
  if (groups.length > 5) bands.push({ key: "__other__", label: "Other", values: BUCKETS.map((_, i) => wave(4_000, i)) });
  return {
    window: { win: "24h", from: FROM_24H, to: NOW, cell_s: 3600, cells: 24 },
    group, metric: "tokens",
    totals: { calls: 1284, tokens_in: 4_100_000, tokens_out: 610_000, spend_micros: 3_120_000, included_calls: 170,
              estimated: true, p95_ms: 18400, failed: 27 },
    spark: { calls: BUCKETS.map((_, i) => wave(60, i)), tokens: BUCKETS.map((_, i) => wave(190_000, i)),
             spend: BUCKETS.map((_, i) => wave(130_000, i)), p95: BUCKETS.map((_, i) => wave(18_000, i)), errors: BUCKETS.map((_, i) => (i % 6 === 0 ? 3 : 1)) },
    series: { buckets: BUCKETS, bands },
    groups,
    failed: [
      { at: iso(NOW_MS - 98 * 60_000), role_key: "software_engineer", stage: "implement", model: "claude-sonnet-4-5", error_word: "Rate limited", retries: 3, run_id: "crun_a1" },
      { at: iso(NOW_MS - 3 * H), role_key: "qa", stage: "review", model: "gpt-5-mini", error_word: "Timed out", retries: 1, run_id: null },
    ],
    routing: { rows: ROUTING_ROWS, total: 12,
               filters: { provider: ["anthropic", "openai"], budget: ["green", "orange"], boundary: ["internal", "public"], outcome: ["selected", "denied"] } },
    etag: `"obs-models-${group}"`,
    ...over,
  };
}
/** Everything ran on a subscription: no metered spend at all. */
export const includedModelsWire = () => modelsWire("model", {
  totals: { calls: 40, tokens_in: 90_000, tokens_out: 12_000, spend_micros: 0, included_calls: 40, estimated: false, p95_ms: 9000, failed: 0 },
  groups: [groupRow({ key: "claude-opus-4", label: "claude-opus-4", calls: 40, tokens_in: 90_000, tokens_out: 12_000, included: true, p95_ms: 9000 })],
  series: { buckets: BUCKETS, bands: [{ key: "claude-opus-4", label: "claude-opus-4", values: BUCKETS.map((_, i) => wave(4_000, i)) }] },
  failed: [],
});
export const emptyModelsWire = () => modelsWire("model", {
  totals: { calls: 0, tokens_in: 0, tokens_out: 0, spend_micros: 0, included_calls: 0, estimated: false, p95_ms: null, failed: 0 },
  groups: [], series: { buckets: BUCKETS, bands: [] }, failed: [], routing: { rows: [], total: 0, filters: {} },
});

export function llmCallsWire(offset = 0, total = 26) {
  const n = Math.max(0, Math.min(20, total - offset));
  return {
    project_id: "p1", count: n, total, limit: 20, offset,
    items: Array.from({ length: n }, (_, i) => {
      const k = offset + i;
      return {
        id: `llm_${k}`, project_id: "p1", created_at: iso(NOW_MS - (k + 1) * 7 * 60_000), code_task_run_id: k % 3 ? "crun_a1" : null,
        stage: k % 2 ? "review" : "implement", role: k % 2 ? "qa" : "software_engineer", engine: "claude_code",
        model: "claude-sonnet-4-5", prompt_tokens: 4000 + k, completion_tokens: 600 + k, total_tokens: 4600 + 2 * k,
        cost_usd_micros: 12_000, price_estimated: k === 0, latency_ms: 8000 + k * 10, retries: k === 2 ? 2 : 0, turns: 1,
        ok: k !== 2, error: k === 2 ? "rate_limit_exceeded" : null, included_in_subscription: false,
      };
    }),
  };
}

export function routeDecisionsWire() {
  return {
    project_id: "p1",
    items: Array.from({ length: 12 }, (_, i) => ({
      id: `mrd_${i + 1}`, project_id: "p1", task_kind: "implement", workload_class: "code", risk_tier: 1,
      selected_source_id: i === 5 ? undefined : "anthropic_api", selected_provider: i === 5 ? undefined : "anthropic",
      selected_model_id: i === 5 ? undefined : "claude-sonnet-4-5", routing_mode: "standard", rejected_sources: [],
      budget_state: "green", data_boundary: "internal", capacity_snapshot: {}, policy_result: {},
      created_at: iso(NOW_MS - (i + 1) * 20 * 60_000),
    })),
  };
}

// ── System ──────────────────────────────────────────────────────────────────────────────────

export function systemWire(over: Record<string, unknown> = {}) {
  return {
    state: "warning", reason: "Memory 78% used",
    machine: {
      scope: "container", scope_label: "This BOSS container's memory cap", level: 1, level_label: "Careful",
      gauges: { memory_total_mb: 8192, memory_available_mb: 1800, swap_used_mb: 0, disk_used_pct: 44, load_per_core: 0.41, cores: 4 },
      bands: {
        memory: { value: 78, unit: "%", band: "warn", word: "High", tone: "amber" },
        cpu: { value: 0.41, unit: "/core", band: "ok", word: "OK", tone: "green" },
        disk: { value: 44, unit: "%", band: "ok", word: "OK", tone: "green" },
        swap: { value: 0, unit: "MB", band: "ok", word: "OK", tone: "green" },
      },
    },
    build: { sha7: "4c1d2e9", built_at: iso(NOW_MS - 2 * H) },
    oom_kills_new: 0,
    work: { queue: 3, leased: 2, running: 2, in_flight: 4, builds_at_once: { label: "2 (auto)" } },
    stalls: {
      strip: Array.from({ length: 96 }, (_, i) => (i === 40 || i === 88 ? 1 : 0)), total: 2,
      rows: [
        { at: iso(NOW_MS - 2 * H), word: "Host stalled", subject: "worker", did: null, shadow: false, run_id: null },
        { at: iso(NOW_MS - 14 * H), word: "Stalled in phase", subject: "invoice build", did: "retry", shadow: true, run_id: "crun_a1" },
      ],
    },
    findings: [ATTENTION_SYSTEM, FINDING_NO_FIX],
    actions: [
      { at: iso(NOW_MS - 50 * 60_000), action: "Freed caches", target: "boss-api", outcome: "1.2 GB freed", automatic: true, improved: true },
      { at: iso(NOW_MS - 6 * H), action: "Paused a build", target: "crun_b2", outcome: "resumed after 4m", automatic: true, improved: null },
    ],
    advice: { title: "Run fewer builds at once", tip: "Two builds hit the memory cap twice today.", href: "?area=observability&view=system" },
    partial: [], etag: "\"obs-system\"",
    ...over,
  };
}
/** No cgroup, no /proc: every gauge is absent. */
export const unmeasuredSystemWire = () => systemWire({
  state: "ok", reason: null, machine: { scope: null, scope_label: null, level: 0, level_label: null, gauges: {}, bands: {} },
  oom_kills_new: null, findings: [], actions: [], advice: null,
  stalls: { strip: Array.from({ length: 96 }, () => 0), total: 0, rows: [] },
});

// ── Sources ─────────────────────────────────────────────────────────────────────────────────

export function sourcesWire(over: Record<string, unknown> = {}) {
  return {
    stack: {
      state: "running", word: "Running", tone: "green", host: { id: "t_live", name: "boss-1" }, grafana_url: "http://grafana.local",
      containers: [{ name: "prometheus", status: "running" }, { name: "grafana", status: "running" }, { name: "otel-collector", status: "running" }],
      checked_at: iso(NOW_MS - 40_000), error: null, can_deploy: true, needs_machine: false,
    },
    scraped: [
      { app: "invoice", env: "prod", scrape_url: "http://10.0.0.3:9100/metrics", dashboard_url: "http://grafana.local/d/invoice", watched: true },
      { app: "orders", env: "dev", scrape_url: "http://10.0.0.4:9102/metrics", dashboard_url: null, watched: false },
    ],
    sources: [
      { id: "src_prom", kind: "prometheus", name: "BOSS Prometheus", url: "http://10.0.0.3:9090", enabled: true },
      { id: "src_loki", kind: "loki", name: "Loki", url: null, enabled: false },
    ],
    gaps: [{ id: "no_logs_backend", label: "No log backend", tip: "No Loki or other log store is registered." }],
    counts: { sources: 2, enabled: 1, scraped: 2 },
    partial: [], etag: "\"obs-sources\"",
    ...over,
  };
}
export const noHostSourcesWire = () => sourcesWire({
  stack: { state: "not_deployed", word: "Not deployed", tone: "grey", host: null, grafana_url: null, containers: [],
           checked_at: null, error: null, can_deploy: false, needs_machine: true },
  scraped: [], gaps: [{ id: "empty_observability", label: "Nothing watched yet", tip: "No sources and no stack." }],
});
export const unknownSourcesWire = () => sourcesWire({
  stack: { state: "unknown", word: "Status unknown", tone: "hollow", host: { id: "t_live", name: "boss-1" }, grafana_url: null,
           containers: [], checked_at: iso(NOW_MS - 5 * 60_000), error: "ssh: connection timed out", can_deploy: true, needs_machine: false },
});

export const CATALOG_WIRE = {
  count: 3,
  items: [
    { id: "prometheus", name: "Prometheus", purpose: "Scrapes and stores metrics", role: "metrics", boss_integration: "stack", deployed_by_boss_stack: true, stars_note: "" },
    { id: "loki", name: "Loki", purpose: "Stores and queries logs", role: "logs", boss_integration: "connector", deployed_by_boss_stack: false, stars_note: "" },
    { id: "tempo", name: "Tempo", purpose: "Stores traces", role: "traces", boss_integration: "connector", deployed_by_boss_stack: false, stars_note: "" },
  ],
  profiles: [
    { id: "minimal", title: "Minimal", description: "", component_ids: ["prometheus"] },
    { id: "web", title: "Web / API", description: "", component_ids: ["prometheus", "loki"] },
  ],
};
export const RECOMMEND_WIRE = {
  workload_profile: "web", title: "Web / API", description: "Metrics and logs for a web service.",
  component_ids: ["prometheus", "loki"], components: CATALOG_WIRE.items.slice(0, 2),
  boss_stack_covers: ["prometheus"], add_alongside_boss_stack: ["loki"], next_steps: [],
};
export const HEALTH_CHECK_WIRE = {
  project_id: "p1", dry_run: true, count: 2, ok_count: 1,
  items: [
    { id: "src_prom", name: "BOSS Prometheus", kind: "prometheus", status: "config_ok", config_ready: true },
    { id: "src_loki", name: "Loki", kind: "loki", status: "misconfigured", config_ready: false, config_error: "base_url is missing" },
  ],
};

// ── Repo and models: model capacity (entitlement sources + official sessions) ───────────────

export function entitlementSourcesWire() {
  return {
    project_id: "p1",
    items: [
      { id: "es_1", project_id: "p1", provider: "anthropic", source_id: "anthropic_api", source_kind: "api_key", status: "active",
        effective_status: "active", auth_method: "api_key", connection_status: "configured", oauth_scopes: [], connection_metadata: {},
        runtime_supported: true, multi_tenant_allowed: true, observed_capacity: { credential_present: true, capacity_state: "available" },
        supported_models: ["claude-sonnet-4-5"] },
      { id: "es_2", project_id: "p1", provider: "openai", source_id: "openai_gateway", source_kind: "gateway", status: "active",
        effective_status: "blocked", auth_method: "gateway_token", connection_status: "not_connected", oauth_scopes: [], connection_metadata: {},
        runtime_supported: true, multi_tenant_allowed: false, observed_capacity: {}, supported_models: [] },
      { id: "es_3", project_id: "p1", provider: "anthropic", source_id: "claude_max", source_kind: "subscription", status: "active",
        effective_status: "needs_connection", auth_method: "oauth_device", connection_status: "not_connected", oauth_scopes: ["user:inference"],
        connection_metadata: { supported_connection_modes: ["claude_cli_login"] }, runtime_supported: true, multi_tenant_allowed: false,
        observed_capacity: {}, supported_models: ["claude-opus-4"] },
      { id: "es_4", project_id: "p1", provider: "openai", source_id: "chatgpt_pro", source_kind: "subscription", status: "active",
        effective_status: "active", auth_method: "oauth_pkce", connection_status: "connected", oauth_scopes: [],
        connection_metadata: {}, runtime_supported: true, multi_tenant_allowed: false,
        observed_capacity: { capacity_state: "available" }, supported_models: ["gpt-5"] },
    ],
  };
}
export function entitlementSessionsWire() {
  return {
    project_id: "p1",
    items: [
      { id: "ecs_1", project_id: "p1", source_id: "claude_max", provider: "anthropic", auth_method: "oauth_device",
        connection_mode: "claude_cli_login", status: "operator_action_required", requested_scopes: ["user:inference"],
        login_command: "claude login", instructions: { operator_action: "Run the login on the server" }, requested_by: "ceo",
        created_at: iso(NOW_MS - 10 * 60_000) },
    ],
  };
}

// ── the fetch mock ──────────────────────────────────────────────────────────────────────────

export type ObsBodies = {
  pulse?: unknown; app?: (targetId: string) => unknown; models?: (group: string) => unknown; system?: unknown; sources?: unknown;
  llmCalls?: (offset: number) => unknown; routes?: unknown; catalog?: unknown; entitlements?: unknown; sessions?: unknown;
  /** Repo and models' board (GET /api/setup), for the `setup` parity rows. */
  setup?: unknown;
  /** GET /v1/reliability/incidents/{id}/response (the Incident card). */
  incident?: unknown;
  /** POST answers by path suffix (`/probe`, `/stack/deploy`, …). */
  writes?: Record<string, { status?: number; body: unknown }>;
  /** App alerts (alerts-fixtures.ts): an app's rules, its history (by `url`), every app's. */
  rules?: (targetId: string) => unknown; history?: (url: URL) => unknown; alerts?: (url: URL) => unknown;
  /** Any other GET by exact path (monitoring: /v1/contact-points, /v1/monitoring/services/x/live, …). */
  gets?: Record<string, (url: URL) => unknown>;
  /** GET /v1/observability/apps/all (screen 15's rows, across projects). */
  appsAll?: unknown;
};
export type Recorded = { url: string; method: string; body: unknown };

/** Install a `fetch` that answers every Observability URL from the fixtures, records each call
 *  in `calls` and each write in `writes`. Unknown URLs answer 404. */
export function mockObsFetch(bodies: ObsBodies = {}) {
  const calls: string[] = [];
  const writes: Recorded[] = [];
  const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status, headers: { "content-type": "application/json", etag: "\"x\"" } });
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push(url);
    const u = new URL(url, "http://console.local");
    const path = u.pathname;
    if (method !== "GET") {
      let body: unknown = init?.body;
      try { body = JSON.parse(String(init?.body)); } catch { /* not json */ }
      writes.push({ url, method, body });
      const hit = Object.entries(bodies.writes ?? {}).find(([suffix]) => path.endsWith(suffix));
      if (hit) return json(hit[1].body, hit[1].status ?? 200);
      if (path.endsWith("/probe")) return json({ word: "Up", tone: "green", checked_at: NOW });
      if (path.endsWith("/stack/deploy")) return json({ ok: true, status: "deploying" });
      if (path.endsWith("/observability-suite/recommend")) return json(RECOMMEND_WIRE);
      if (path.endsWith("/observability-suite/health-check")) return json(HEALTH_CHECK_WIRE);
      return json({ ok: true });
    }
    if (bodies.gets?.[path]) return json(bodies.gets[path](u));
    if (/^\/v1\/apps\/[^/]+\/alert-rules$/.test(path)) {
      const id = decodeURIComponent(path.split("/")[3] ?? "");
      return json(bodies.rules ? bodies.rules(id) : rulesWire(id));
    }
    if (/^\/v1\/apps\/[^/]+\/alerts$/.test(path)) return json(bodies.history ? bodies.history(u) : historyWire());
    if (path === "/v1/alerts") return json(bodies.alerts ? bodies.alerts(u) : allAlertsWire());
    if (path === "/v1/observability/apps") return json(bodies.pulse ?? pulseWire());
    if (path === "/v1/observability/apps/all") return json(bodies.appsAll ?? appsAllWire());
    if (path.startsWith("/v1/observability/apps/")) {
      const id = decodeURIComponent(path.split("/")[4] ?? "");
      return json(bodies.app ? bodies.app(id) : appWire(id));
    }
    if (path === "/v1/observability/models") {
      const g = u.searchParams.get("group") ?? "model";
      return json(bodies.models ? bodies.models(g) : modelsWire(g));
    }
    if (path === "/v1/observability/system") return json(bodies.system ?? systemWire());
    if (path === "/v1/observability/sources") return json(bodies.sources ?? sourcesWire());
    if (path === "/v1/llm-calls") {
      const off = Number(u.searchParams.get("offset") ?? 0);
      return json(bodies.llmCalls ? bodies.llmCalls(off) : llmCallsWire(off));
    }
    if (path === "/v1/model-route-decisions") return json(bodies.routes ?? routeDecisionsWire());
    if (path === "/v1/observability-suite/catalog") return json(bodies.catalog ?? CATALOG_WIRE);
    if (path === "/v1/entitlements/sources") return json(bodies.entitlements ?? entitlementSourcesWire());
    if (path === "/v1/entitlement-connection-sessions") return json(bodies.sessions ?? entitlementSessionsWire());
    if (path === "/api/setup") return json(bodies.setup ?? board());
    if (/^\/v1\/reliability\/incidents\/[^/]+\/response$/.test(path)) return json(bodies.incident ?? incidentResponseWire());
    if (/^\/v1\/reliability\/incidents\/[^/]+\/report$/.test(path)) return json(INCIDENT_REPORT_WIRE);
    return new Response("{}", { status: 404 });
  });
  vi.stubGlobal("fetch", fn);
  return { fn, calls, writes };
}

/** Screen 15's rows: the Pulse's apps of project p1 plus another project's app (a firing one). */
export function appsAllWire(over: Record<string, unknown> = {}) {
  const mine = PULSE_ROWS.map((r) => ({ ...r, firing: r.target_id === "t_down" ? 2 : 0,
    project: { id: "p1", name: "HelpDesk AI Demo", initials: "HD" },
    health_href: `?project_id=p1&area=project&tab=health&app=${r.target_id}`,
    drawer_href: `?project_id=p1&area=observability&app=${r.target_id}` }));
  const other = { ...PULSE_ROWS[3], target_id: "t_np", app: "northpeak", env: "preview", firing: 1, state: "degraded", word: "Slow",
    project: { id: "p2", name: "NorthPeak Retail", initials: "NP" },
    health_href: "?project_id=p2&area=project&tab=health&app=t_np", drawer_href: "?project_id=p2&area=observability&app=t_np" };
  return { rows: [...mine, other], counts: {}, projects: 2, partial: [], ...over };
}
