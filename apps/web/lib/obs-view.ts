/**
 * Observability: the frozen contract (infra-observability spec §4.9-§4.13, §6.2; Phase 0).
 *
 * engine/boss_engine/services/obs_view.py holds the same shapes. Four tabs, one window control
 * (`win`, in the URL) that drives every number and chart on a tab. A Pulse cell with no probes
 * is a gap (`n === 0`), drawn outlined, never green. Spend is metered spend only; `estimated`
 * says some prices were estimated; there is no provider column because llm_calls has none.
 *
 * System is the stability work's real data (admission gauges, system-doctor findings drawn as
 * AttentionCard `system_health` items, the doctor's recent actions), composed server-side.
 */

import { arr, num, obj, opt, str, usePolled, type Polled } from "./factory-floor";
import { parseAttention, type AttentionItem } from "./agents-live-board";
import { parseWord, toneOf, type Tone, type Word } from "./infra-view";
import { ALERT_SOURCES, parseGrafana, type AlertSource, type Grafana, type Sev } from "./app-alerts";

const optNum = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const optStr = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const ref = (v: unknown): { id: string; name: string } | null => {
  const o = obj(v);
  return o.id || o.name ? { id: str(o.id), name: str(o.name) } : null;
};

// ── the window (§3.2, §4.9) ─────────────────────────────────────────────────────────────────

export type Win = "24h" | "7d" | "30d";
export const WINDOWS: Win[] = ["24h", "7d", "30d"];
export const winOf = (v: unknown): Win => (WINDOWS.includes(v as Win) ? (v as Win) : "24h");
export type WindowInfo = { win: Win; from: string | null; to: string | null; cellS: number; cells: number };
export function parseWindow(w: unknown): WindowInfo {
  const o = obj(w);
  return { win: winOf(o.win), from: optStr(o.from), to: optStr(o.to), cellS: num(o.cell_s), cells: num(o.cells) };
}

// ── Apps: the Pulse (§4.9) ──────────────────────────────────────────────────────────────────

/** ok of n probes in the bucket, and its p95 (ms) when latency was recorded. n === 0: a gap. */
export type PulseCell = { ok: number; n: number; p95: number | null };
export const parsePulseStrip = (v: unknown): PulseCell[] =>
  arr(v).map((c) => { const a = arr(c); return { ok: num(a[0]), n: num(a[1]), p95: optNum(a[2]) }; });

export type Pin = { at: string; sha7: string | null; word: string; tone: Tone; ref: string | null };
const parsePin = (w: unknown): Pin => {
  const o = obj(w);
  return { at: str(o.at), sha7: optStr(o.sha7), word: str(o.word), tone: toneOf(o.tone), ref: optStr(o.ref) };
};

export type HealthState = "up" | "degraded" | "down" | "unknown" | "not_deployed";
export type PulseRow = {
  targetId: string; app: string; env: string; host: { id: string; name: string } | null;
  state: HealthState; word: string; tone: Tone; since: string | null;
  uptime: number | null; p95Ms: number | null; strip: PulseCell[]; pins: Pin[]; incidents: number;
};
const HEALTH = new Set(["up", "degraded", "down", "unknown", "not_deployed"]);
export function parsePulseRow(w: unknown): PulseRow {
  const o = obj(w);
  return {
    targetId: str(o.target_id), app: str(o.app), env: str(o.env), host: ref(o.host),
    state: (HEALTH.has(str(o.state)) ? str(o.state) : "unknown") as HealthState, word: str(o.word, "No signal"),
    tone: toneOf(o.tone), since: optStr(o.since), uptime: optNum(o.uptime), p95Ms: optNum(o.p95_ms),
    strip: parsePulseStrip(o.strip), pins: arr(o.pins).map(parsePin), incidents: num(o.incidents),
  };
}
export type NotWatched = { app: string; env: string; scrapeUrl: string | null; dashboardUrl: string | null };
export type Pulse = {
  counts: { up: number; slow: number; down: number; noSignal: number; notDeployed: number };
  rows: PulseRow[]; notWatched: NotWatched[]; window: WindowInfo; attention: AttentionItem[]; partial: string[];
  /** Metered model spend in the last 24 h, for the header (`$3.12 · 24h`); shown only when > 0. */
  spend24hMicros: number; spend24hEstimated: boolean;
  /** The header's Grafana ↗: a link only once BOSS checked that Grafana answers, else why. */
  grafana: Grafana;
  /** Alerts firing now and not held back: the Alerts tab's count and dot. */
  alertsFiring: number;
};
export function parsePulse(w: unknown): Pulse {
  const o = obj(w);
  const c = obj(o.counts);
  return {
    counts: { up: num(c.up), slow: num(c.slow), down: num(c.down), noSignal: num(c.no_signal), notDeployed: num(c.not_deployed) },
    rows: arr(o.rows).map(parsePulseRow),
    notWatched: arr(o.not_watched).map((x) => { const y = obj(x); return { app: str(y.app), env: str(y.env), scrapeUrl: optStr(y.scrape_url), dashboardUrl: optStr(y.dashboard_url) }; }),
    window: parseWindow(o.window), attention: arr(o.attention).map(parseAttention), partial: arr(o.partial).map(String),
    spend24hMicros: num(o.spend_24h_micros), spend24hEstimated: o.spend_24h_estimated === true,
    grafana: parseGrafana(o.grafana), alertsFiring: num(o.alerts_firing),
  };
}

export type Incident = { start: string; end: string | null; durationS: number | null; firstError: string | null;
  deployBefore: { ref: string; sha7: string | null } | null };
export type SeriesPoint = { at: string; p95: number | null; avg: number | null; ok: number; n: number };
export type AppDetail = {
  targetId: string; app: string; env: string; state: HealthState; word: string; tone: Tone; since: string | null;
  facts: {
    uptime24h: number | null; uptime7d: number | null; p95Ms: number | null; avgMs: number | null;
    lastProbeAt: string | null; probes: number; failed: number; buildSha7: string | null; deployedAt: string | null;
    observedVersion: string | null; servingMismatch: boolean; host: { id: string; name: string } | null;
    openUrl: string | null; dashboardUrl: string | null; lastError: string | null;
    /** Why there is no Dashboard ↗ ("Grafana isn't answering on its host"). */
    dashboardReason: string | null; grafana: Grafana;
  };
  series: SeriesPoint[]; incidents: Incident[]; pins: Pin[]; window: WindowInfo;
  metrics: AppMetrics;
};
/** The app's OWN /metrics (engine app_metrics.metrics_view): request rate, 5xx error rate and
 *  p50/p95 over the alert window, the last scrape in words, and the error-rate alert. */
export type AlertState = "firing" | "ok" | "quiet" | "off";
export type AppMetrics = {
  declared: boolean; word: string; tip: string | null; path: string | null; via: string | null;
  state: "ok" | "failing" | "waiting" | null; lastScrapeAt: string | null; lastOkAt: string | null;
  route: string | null; url: string | null; naming: string | null; intervalS: number | null;
  requests: number | null; errors: number | null; errorRate: number | null; ratePerMin: number | null;
  p50Ms: number | null; p95Ms: number | null; windowMinutes: number | null;
  alert: { state: AlertState; word: string; incidentId: string | null; incidentStatus: string | null; since: string | null;
           rule: { errorRatePercent: number | null; windowMinutes: number | null; minRequests: number | null; enabled: boolean } } | null;
};
const ALERT_STATES = new Set(["firing", "ok", "quiet", "off"]);
export function parseAppMetrics(w: unknown): AppMetrics {
  const o = obj(w);
  const a = o.alert ? obj(o.alert) : null;
  const r = a ? obj(a.rule) : {};
  const st = str(o.state);
  return {
    declared: o.declared === true, word: str(o.word) || "Not declared", tip: optStr(o.tip), path: optStr(o.path),
    via: optStr(o.via), state: st === "ok" || st === "failing" || st === "waiting" ? st : null,
    lastScrapeAt: optStr(o.last_scrape_at), lastOkAt: optStr(o.last_ok_at), route: optStr(o.route), url: optStr(o.url),
    naming: optStr(o.naming), intervalS: optNum(o.interval_s), requests: optNum(o.requests), errors: optNum(o.errors),
    errorRate: optNum(o.error_rate), ratePerMin: optNum(o.rate_per_min), p50Ms: optNum(o.p50_ms), p95Ms: optNum(o.p95_ms),
    windowMinutes: optNum(o.window_minutes),
    alert: a ? {
      state: (ALERT_STATES.has(str(a.state)) ? str(a.state) : "quiet") as AlertState, word: str(a.word),
      incidentId: optStr(a.incident_id), incidentStatus: optStr(a.incident_status), since: optStr(a.since),
      rule: { errorRatePercent: optNum(r.error_rate_percent), windowMinutes: optNum(r.window_minutes),
              minRequests: optNum(r.min_requests), enabled: r.enabled !== false },
    } : null,
  };
}
export function parseAppDetail(w: unknown): AppDetail {
  const o = obj(w);
  const f = obj(o.facts);
  return {
    targetId: str(o.target_id), app: str(o.app), env: str(o.env),
    state: (HEALTH.has(str(o.state)) ? str(o.state) : "unknown") as HealthState, word: str(o.word), tone: toneOf(o.tone),
    since: optStr(o.since),
    facts: {
      uptime24h: optNum(f.uptime_24h), uptime7d: optNum(f.uptime_7d), p95Ms: optNum(f.p95_ms), avgMs: optNum(f.avg_ms),
      lastProbeAt: optStr(f.last_probe_at), probes: num(f.probes), failed: num(f.failed), buildSha7: optStr(f.build_sha7),
      deployedAt: optStr(f.deployed_at), observedVersion: optStr(f.observed_version), servingMismatch: f.serving_mismatch === true,
      host: ref(f.host), openUrl: optStr(f.open_url), dashboardUrl: optStr(f.dashboard_url), lastError: optStr(f.last_error),
      dashboardReason: optStr(f.dashboard_reason), grafana: parseGrafana(f.grafana),
    },
    series: arr(o.series).slice(0, 288).map((p) => { const x = obj(p); return { at: str(x.at), p95: optNum(x.p95), avg: optNum(x.avg), ok: num(x.ok), n: num(x.n) }; }),
    incidents: arr(o.incidents).map((i) => {
      const x = obj(i);
      const d = x.deploy_before ? obj(x.deploy_before) : null;
      return { start: str(x.start), end: optStr(x.end), durationS: optNum(x.duration_s), firstError: optStr(x.first_error),
               deployBefore: d ? { ref: str(d.ref), sha7: optStr(d.sha7) } : null };
    }),
    pins: arr(o.pins).map(parsePin), window: parseWindow(o.window),
    metrics: parseAppMetrics(o.metrics),
  };
}

// ── Models (§4.10) ──────────────────────────────────────────────────────────────────────────

export type Group = "model" | "stage" | "role" | "engine";
export const GROUPS: { key: Group; label: string; head: string }[] = [
  { key: "model", label: "Model", head: "MODELS" }, { key: "stage", label: "Stage", head: "STAGES" },
  { key: "role", label: "Agent", head: "AGENTS" }, { key: "engine", label: "Runner", head: "RUNNERS" },
];
export type Metric = "tokens" | "calls" | "spend" | "latency";
export const METRICS: { key: Metric; label: string }[] = [
  { key: "tokens", label: "Tokens" }, { key: "calls", label: "Calls" }, { key: "spend", label: "Spend" }, { key: "latency", label: "Latency" },
];
export const groupOf = (v: unknown): Group => (GROUPS.some((g) => g.key === v) ? (v as Group) : "model");
export const metricOf = (v: unknown): Metric => (METRICS.some((m) => m.key === v) ? (v as Metric) : "tokens");

export type ModelGroupRow = {
  key: string; label: string; roleKey: string | null; guest: boolean;
  calls: number; tokensIn: number; tokensOut: number; spendMicros: number; included: boolean; estimated: boolean;
  avgMs: number | null; p95Ms: number | null; failed: number; retries: number; trend: number[];
};
export type FailedCall = { at: string; roleKey: string | null; stage: string | null; model: string | null;
  errorWord: string; retries: number; runId: string | null };
export type RoutingRow = { at: string; provider: string | null; budget: string | null; boundary: string | null;
  outcome: string | null; model: string | null; id: string };
export type Models = {
  window: WindowInfo; group: Group; metric: Metric;
  totals: { calls: number; tokensIn: number; tokensOut: number; spendMicros: number; includedCalls: number;
            estimated: boolean; p95Ms: number | null; failed: number };
  spark: { calls: number[]; tokens: number[]; spend: number[]; p95: number[]; errors: number[] };
  series: { buckets: string[]; bands: { key: string; label: string; values: number[] }[] };
  groups: ModelGroupRow[]; failed: FailedCall[];
  routing: { rows: RoutingRow[]; total: number; filters: { provider: string[]; budget: string[]; boundary: string[]; outcome: string[] } };
};
const nums = (v: unknown): number[] => arr(v).map((x) => num(x));
export function parseModels(w: unknown): Models {
  const o = obj(w);
  const t = obj(o.totals);
  const sp = obj(o.spark);
  const se = obj(o.series);
  const ro = obj(o.routing);
  const rf = obj(ro.filters);
  return {
    window: parseWindow(o.window), group: groupOf(o.group), metric: metricOf(o.metric),
    totals: { calls: num(t.calls), tokensIn: num(t.tokens_in), tokensOut: num(t.tokens_out), spendMicros: num(t.spend_micros),
              includedCalls: num(t.included_calls), estimated: t.estimated === true, p95Ms: optNum(t.p95_ms), failed: num(t.failed) },
    spark: { calls: nums(sp.calls), tokens: nums(sp.tokens), spend: nums(sp.spend), p95: nums(sp.p95), errors: nums(sp.errors) },
    series: { buckets: arr(se.buckets).map(String),
              bands: arr(se.bands).map((b) => { const x = obj(b); return { key: str(x.key), label: str(x.label, str(x.key)), values: nums(x.values) }; }) },
    groups: arr(o.groups).slice(0, 50).map((g) => {
      const x = obj(g);
      return { key: str(x.key), label: str(x.label, str(x.key)), roleKey: optStr(x.role_key), guest: x.guest === true,
               calls: num(x.calls), tokensIn: num(x.tokens_in), tokensOut: num(x.tokens_out), spendMicros: num(x.spend_micros),
               included: x.included === true, estimated: x.estimated === true, avgMs: optNum(x.avg_ms), p95Ms: optNum(x.p95_ms),
               failed: num(x.failed), retries: num(x.retries), trend: nums(x.trend) };
    }),
    failed: arr(o.failed).slice(0, 5).map((f) => {
      const x = obj(f);
      return { at: str(x.at), roleKey: optStr(x.role_key), stage: optStr(x.stage), model: optStr(x.model),
               errorWord: str(x.error_word, "Failed"), retries: num(x.retries), runId: optStr(x.run_id) };
    }),
    routing: {
      rows: arr(ro.rows).map((r) => { const x = obj(r); return { id: str(x.id), at: str(x.at), provider: optStr(x.provider), budget: optStr(x.budget),
                                                                 boundary: optStr(x.boundary), outcome: optStr(x.outcome), model: optStr(x.model) }; }),
      total: num(ro.total),
      filters: { provider: arr(rf.provider).map(String), budget: arr(rf.budget).map(String),
                 boundary: arr(rf.boundary).map(String), outcome: arr(rf.outcome).map(String) },
    },
  };
}

/** One model call, from GET /v1/llm-calls (the group drawer's RECENT CALLS). */
export type LlmCall = { id: string; at: string; roleKey: string | null; stage: string | null; model: string | null;
  engine: string | null; tokensIn: number; tokensOut: number; latencyMs: number | null; ok: boolean; retries: number;
  runId: string | null; spendMicros: number; included: boolean; estimated: boolean; error: string | null };
export function parseLlmCall(w: unknown): LlmCall {
  const o = obj(w);
  return {
    id: str(o.id), at: str(o.created_at, str(o.at)), roleKey: optStr(o.role), stage: optStr(o.stage), model: optStr(o.model),
    engine: optStr(o.engine), tokensIn: num(o.prompt_tokens), tokensOut: num(o.completion_tokens), latencyMs: optNum(o.latency_ms),
    ok: o.ok !== false, retries: num(o.retries), runId: optStr(o.code_task_run_id), spendMicros: num(o.cost_usd_micros),
    included: o.included_in_subscription === true, estimated: o.price_estimated === true, error: optStr(o.error),
  };
}

// ── System (§4.11; the stability work's data) ───────────────────────────────────────────────

export type Band = { value: number | null; unit: string; band: string; word: string; tone: Tone; tip?: string };
export type System = {
  state: "ok" | "warning" | "critical"; reason: string | null;
  machine: {
    scope: string | null; scopeLabel: string | null; level: number; levelLabel: string | null;
    bands: { memory: Band | null; cpu: Band | null; disk: Band | null; swap: Band | null };
    gauges: Record<string, number>;
  };
  build: { sha7: string | null; builtAt: string | null };
  oomKillsNew: number | null;
  work: { queue: number; leased: number; running: number; inFlight: number; buildsAtOnce: string | null };
  stalls: { strip: number[]; rows: { at: string; word: string; subject: string | null; did: string | null; shadow: boolean; runId: string | null }[]; total: number };
  findings: AttentionItem[];
  actions: { at: string; action: string; target: string | null; outcome: string | null; automatic: boolean; improved: boolean | null }[];
  /** The host advice line (the HostAdviceBanner Workflows and Factory show), when there is one. */
  advice: { title: string; tip: string; href: string | null } | null;
  partial: string[];
};
const parseBand = (w: unknown): Band | null => {
  if (!w) return null;
  const o = obj(w);
  const word: Word = parseWord({ word: o.word, tone: o.tone }, "OK");
  return { value: optNum(o.value), unit: str(o.unit), band: str(o.band, "ok"), word: word.word, tone: word.tone, ...(opt(o.tip) ? { tip: str(o.tip) } : {}) };
};
export function parseSystem(w: unknown): System {
  const o = obj(w);
  const m = obj(o.machine);
  const b = obj(m.bands);
  const g: Record<string, number> = {};
  for (const [k, v] of Object.entries(obj(m.gauges))) if (typeof v === "number") g[k] = v;
  const bu = obj(o.build);
  const wk = obj(o.work);
  const st = obj(o.stalls);
  const state = str(o.state, "ok");
  return {
    state: (state === "critical" || state === "warning" ? state : "ok"), reason: optStr(o.reason),
    machine: { scope: optStr(m.scope), scopeLabel: optStr(m.scope_label), level: num(m.level), levelLabel: optStr(m.level_label),
               bands: { memory: parseBand(b.memory), cpu: parseBand(b.cpu), disk: parseBand(b.disk), swap: parseBand(b.swap) }, gauges: g },
    build: { sha7: optStr(bu.sha7), builtAt: optStr(bu.built_at) },
    oomKillsNew: optNum(o.oom_kills_new),
    work: { queue: num(wk.queue), leased: num(wk.leased), running: num(wk.running), inFlight: num(wk.in_flight),
            buildsAtOnce: optStr(obj(wk.builds_at_once).label) },
    stalls: { strip: nums(st.strip), total: num(st.total),
              rows: arr(st.rows).slice(0, 8).map((r) => { const x = obj(r); return { at: str(x.at), word: str(x.word), subject: optStr(x.subject),
                                                                                    did: optStr(x.did), shadow: x.shadow === true, runId: optStr(x.run_id) }; }) },
    findings: arr(o.findings).map(parseAttention),
    actions: arr(o.actions).slice(0, 8).map((a) => { const x = obj(a); return { at: str(x.at), action: str(x.action), target: optStr(x.target),
                                                                               outcome: optStr(x.outcome), automatic: x.automatic === true,
                                                                               improved: typeof x.improved === "boolean" ? x.improved : null }; }),
    advice: o.advice ? { title: str(obj(o.advice).title), tip: str(obj(o.advice).tip), href: optStr(obj(o.advice).href) } : null,
    partial: arr(o.partial).map(String),
  };
}

// ── Sources (§4.13) ─────────────────────────────────────────────────────────────────────────

export type Sources = {
  stack: { state: string; word: string; tone: Tone; host: { id: string; name: string } | null; grafanaUrl: string | null;
           containers: { name: string; status: string }[]; checkedAt: string | null; error: string | null;
           canDeploy: boolean; needsMachine: boolean;
           /** The target the stack borrows a host + credential from: POST /v1/observability/stack/deploy's host_target_id. */
           hostTargetId: string | null };
  scraped: { app: string; env: string; scrapeUrl: string | null; dashboardUrl: string | null; watched: boolean }[];
  sources: { id: string; kind: string; name: string; url: string | null; enabled: boolean }[];
  gaps: { id: string; label: string; tip: string }[];
  counts: Record<string, number>; partial: string[];
};
export function parseSources(w: unknown): Sources {
  const o = obj(w);
  const s = obj(o.stack);
  const counts: Record<string, number> = {};
  for (const [k, v] of Object.entries(obj(o.counts))) counts[k] = num(v);
  return {
    stack: { state: str(s.state, "unknown"), word: str(s.word, "Status unknown"), tone: toneOf(s.tone), host: ref(s.host),
             grafanaUrl: optStr(s.grafana_url),
             containers: arr(s.containers).map((c) => { const x = obj(c); return { name: str(x.name), status: str(x.status) }; }),
             checkedAt: optStr(s.checked_at), error: optStr(s.error), canDeploy: s.can_deploy === true, needsMachine: s.needs_machine === true,
             hostTargetId: optStr(s.host_target_id) },
    scraped: arr(o.scraped).map((x) => { const y = obj(x); return { app: str(y.app), env: str(y.env), scrapeUrl: optStr(y.scrape_url),
                                                                     dashboardUrl: optStr(y.dashboard_url), watched: y.watched !== false }; }),
    sources: arr(o.sources).map((x) => { const y = obj(x); return { id: str(y.id), kind: str(y.kind), name: str(y.name), url: optStr(y.url), enabled: y.enabled !== false }; }),
    gaps: arr(o.gaps).map((x) => { const y = obj(x); return { id: str(y.id), label: str(y.label), tip: str(y.tip) }; }),
    counts, partial: arr(o.partial).map(String),
  };
}

// ── URL state (§4.1) ────────────────────────────────────────────────────────────────────────

export type ObsTab = "apps" | "alerts" | "notify" | "models" | "system" | "sources" | "scorecards" | "installs";
export const OBS_TABS: { key: ObsTab; label: string; tip: string }[] = [
  // Screen 15's filters first — Apps · BOSS platform · Alert sources · Contact points — then the
  // rest of what Observability already does (§13: nothing is removed).
  { key: "apps", label: "Apps", tip: "Every app: is it up, how fast, what is firing" },
  { key: "system", label: "BOSS platform", tip: "BOSS's own machine, its queue and stalls" },
  { key: "sources", label: "Alert sources", tip: "Monitoring stack, telemetry and alert sources" },
  { key: "notify", label: "Contact points", tip: "Who is told, how, and when" },
  { key: "alerts", label: "Alerts", tip: "Every app's alerts: firing now and recent" },
  { key: "models", label: "Models", tip: "Every model call: tokens, time, errors, spend" },
  { key: "scorecards", label: "Scorecards", tip: "How every run turned out, by role, workflow, harness, model" },
  // Collector mode only (obs-home filters it): installs reporting to this BOSS.
  { key: "installs", label: "Installs", tip: "Installs reporting to this collector" },
];
/** `atab=alerts` opens the app drawer on its Alerts tab (Factory ⋯ › Alerts links there);
 *  `aapp`, `astate`, `asev`, `asrc`, `aflap`, `aack`, `aatt` are Observability › Alerts' filters. */
export type DrawerTab = "overview" | "alerts";
export type ObsUrl = { view: ObsTab; win: Win; group: Group; metric: Metric; app: string | null; model: string | null; q: string;
  atab: DrawerTab; aapp: string | null; astate: "firing" | "resolved" | "all"; asev: Sev | null;
  asrc: AlertSource | null; aflap: boolean | null; aack: boolean | null;
  /** `aatt=1`: Needs attention only (firing and not acknowledged). */
  aatt: boolean;
  /** The incident opened from an alert. Null: the app's current incident. */
  inc: string | null };
const ALERT_SOURCE_KEYS = new Set<string>(ALERT_SOURCES.map((s) => s.key));
const triOf = (v: string | null): boolean | null => (v === "true" ? true : v === "false" ? false : null);
const OBS_TAB_KEYS = new Set<string>(OBS_TABS.map((t) => t.key));
/** `tab=` is accepted as well as `view=`: Govern links to `?area=observability&tab=models`. */
export function readObsUrl(search: string): ObsUrl {
  const p = new URLSearchParams(search);
  const view = p.get("view") ?? p.get("tab") ?? "";
  // Govern's `tab=models` link (no `view=`, no `win=`) means the month.
  const govern = !p.get("view") && p.get("tab") === "models" && !p.get("win");
  return {
    view: (OBS_TAB_KEYS.has(view) ? view : "apps") as ObsTab, win: govern ? "30d" : winOf(p.get("win")), group: groupOf(p.get("group")),
    metric: metricOf(p.get("metric")), app: p.get("app"), model: p.get("model"), q: p.get("q") ?? "",
    atab: p.get("atab") === "alerts" ? "alerts" : "overview", aapp: p.get("aapp"),
    astate: p.get("astate") === "resolved" || p.get("astate") === "all" ? (p.get("astate") as "resolved" | "all") : "firing",
    asev: /^sev[1-4]$/.test(p.get("asev") ?? "") ? (p.get("asev") as Sev) : null,
    asrc: ALERT_SOURCE_KEYS.has(p.get("asrc") ?? "") ? (p.get("asrc") as AlertSource) : null,
    aflap: triOf(p.get("aflap")), aack: triOf(p.get("aack")), aatt: p.get("aatt") === "1",
    inc: p.get("inc"),
  };
}

// ── fetch urls + hooks ──────────────────────────────────────────────────────────────────────

const q = (projectId: string, extra: Record<string, string | number | null | undefined> = {}) => {
  const p = new URLSearchParams({ project_id: projectId });
  for (const [k, v] of Object.entries(extra)) if (v !== null && v !== undefined && v !== "") p.set(k, String(v));
  return p.toString();
};
export const pulseUrl = (projectId: string, win: Win, cells?: number) => `/v1/observability/apps?${q(projectId, { win, cells })}`;
export const appUrl = (projectId: string, targetId: string, win: Win) => `/v1/observability/apps/${encodeURIComponent(targetId)}?${q(projectId, { win })}`;
export const modelsUrl = (projectId: string, win: Win, group: Group, metric: Metric) => `/v1/observability/models?${q(projectId, { win, group, metric })}`;
export const systemUrl = (projectId: string) => `/v1/observability/system?${q(projectId)}`;
export const sourcesUrl = (projectId: string, fresh = false) => `/v1/observability/sources?${q(projectId, { fresh: fresh ? 1 : null })}`;
export const llmCallsUrl = (projectId: string, f: { since?: string; model?: string; stage?: string; role?: string; engine?: string; offset?: number; limit?: number }) =>
  `/v1/llm-calls?${q(projectId, f)}`;

export const PULSE_POLL_MS = 30_000;
export const APP_POLL_MS = 30_000;
export const MODELS_POLL_MS = { "24h": 60_000, "7d": 300_000, "30d": 300_000 } as const;
export const SYSTEM_POLL_MS = 10_000;
export const SOURCES_POLL_MS = 60_000;

export const usePulse = (projectId: string, win: Win, cells?: number): Polled<Pulse> =>
  usePolled(projectId ? pulseUrl(projectId, win, cells) : null, parsePulse, () => PULSE_POLL_MS);
export const useAppDetail = (projectId: string, targetId: string | null, win: Win): Polled<AppDetail> =>
  usePolled(projectId && targetId ? appUrl(projectId, targetId, win) : null, parseAppDetail, () => APP_POLL_MS);
export const useModels = (projectId: string, win: Win, group: Group, metric: Metric): Polled<Models> =>
  usePolled(projectId ? modelsUrl(projectId, win, group, metric) : null, parseModels, () => MODELS_POLL_MS[win]);
export const useSystem = (projectId: string): Polled<System> =>
  usePolled(projectId ? systemUrl(projectId) : null, parseSystem, () => SYSTEM_POLL_MS);
export const useSources = (projectId: string): Polled<Sources> =>
  usePolled(projectId ? sourcesUrl(projectId) : null, parseSources, () => SOURCES_POLL_MS);

// ── money and units (§5.1) ──────────────────────────────────────────────────────────────────

/** Dollars with 2 decimals, `<$0.01` below a cent. Never micros, never `$0` (callers show
 *  `Included in plan` instead when spend is zero because it ran on a subscription). */
export function usd(micros: number): string {
  const d = micros / 1_000_000;
  if (d > 0 && d < 0.01) return "<$0.01";
  return `$${d.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
/** 1,284 · 4.1M · 610k */
export function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  return n.toLocaleString("en-US");
}
/** 212 ms · 2.4 s · 1m 4s */
export function ms(v: number | null): string {
  if (v === null) return "—";
  if (v < 1000) return `${Math.round(v)} ms`;
  if (v < 60_000) return `${(v / 1000).toFixed(v < 10_000 ? 1 : 0)} s`;
  const s = Math.round(v / 1000);
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}
/** A measured ratio (uptime, error rate), never progress. */
export function pct(ratio: number | null, digits = 1): string {
  if (ratio === null) return "—";
  return `${(ratio * 100).toFixed(digits)}%`;
}
