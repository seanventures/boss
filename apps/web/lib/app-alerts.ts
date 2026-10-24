/**
 * App alerts (BOSSAPP-598): the wire shapes of engine/boss_engine/services/app_alerts_view.py.
 *
 *   GET  /v1/apps/{id}/alert-rules   rules (Prometheus shape + words), signal sources, changes
 *   GET  /v1/apps/{id}/alerts        one app's history          GET /v1/alerts   every app's
 *   PUT  /v1/apps/{id}/alert-rules   a person's edit (console wins; audited; consent for Respond)
 *   POST /v1/apps/{id}/alert-rules/{rule}/silence · /alert-silences/{id}/end · /alerts/{id}/triage
 *   POST /v1/apps/{id}/signal-sources/{source}/rotate · /v1/observability/grafana/check
 *
 * Every fired alert carries its PromQL, its value at breach, a Grafana Explore link (only when
 * BOSS checked that Grafana answers; otherwise the reason), its runbook and the agent's triage.
 */
import { arr, num, obj, str, usePolled, type Polled } from "./factory-floor";

const optNum = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const optStr = (v: unknown): string | null => (typeof v === "string" && v ? v : null);

export type Tone = "red" | "amber" | "green" | "grey";
const toneOf = (v: unknown): Tone => (v === "red" || v === "amber" || v === "green" ? v : "grey");

export type Runbook = { url: string; kind: "builtin" | "link" | "repo" | "none"; title: string; markdown: string | null; href: string | null };
const parseRunbook = (w: unknown): Runbook => {
  const o = obj(w);
  const k = str(o.kind);
  return { url: str(o.url), kind: (k === "builtin" || k === "link" || k === "repo" ? k : "none"), title: str(o.title, "No runbook"),
           markdown: optStr(o.markdown), href: optStr(o.href) };
};

// ── severity SEV1–SEV4 ──────────────────────────────────────────────────────────────────────
export type Sev = "sev1" | "sev2" | "sev3" | "sev4";
export type SevInfo = { key: Sev; label: string; word: string; meaning: string; routing: string };
/** The catalog the API sends as `severities`; this copy is the fallback for older answers. */
export const SEVERITIES: SevInfo[] = [
  { key: "sev1", label: "SEV1", word: "Critical", meaning: "Customer-facing outage", routing: "Pages: high urgency, notifies at once, ignores quiet hours" },
  { key: "sev2", label: "SEV2", word: "Major", meaning: "Major degradation", routing: "High urgency: notifies at once" },
  { key: "sev3", label: "SEV3", word: "Minor", meaning: "Minor or partial problem", routing: "Low urgency: waits out quiet hours" },
  { key: "sev4", label: "SEV4", word: "Info", meaning: "Informational", routing: "Low urgency: console and the daily digest" },
];
const SEV_KEYS = new Set<string>(["sev1", "sev2", "sev3", "sev4"]);
/** `sev` when sent, else the raw severity label: critical→SEV1, warning→SEV3, info→SEV4. */
export function sevOf(sev: unknown, severity?: unknown): Sev {
  const s = typeof sev === "string" ? sev.toLowerCase() : "";
  if (SEV_KEYS.has(s)) return s as Sev;
  const raw = typeof severity === "string" ? severity.toLowerCase() : "";
  if (SEV_KEYS.has(raw)) return raw as Sev;
  return raw === "critical" ? "sev1" : raw === "info" ? "sev4" : "sev3";
}
export const parseSeverities = (w: unknown): SevInfo[] => {
  const got = arr(w).map((x) => { const o = obj(x); return { key: sevOf(o.key), label: str(o.label), word: str(o.word),
                                                             meaning: str(o.meaning), routing: str(o.routing) }; })
    .filter((x) => x.label);
  return got.length === 4 ? got : SEVERITIES;
};

/** A status chip, as the API words it ("Acknowledged · Param", "Resolved"). */
export type StatusKey = "firing" | "acknowledged" | "pending" | "resolved" | "silenced" | "maintenance" | "recovered";
export type Status = { key: StatusKey; word: string; tone: "red" | "amber" | "grey" | "green" | "muted"; tip: string | null };
const STATUS_KEYS = new Set<string>(["firing", "acknowledged", "pending", "resolved", "silenced", "maintenance", "recovered"]);
const STATUS_TONE: Record<StatusKey, Status["tone"]> = { firing: "red", acknowledged: "amber", pending: "grey", resolved: "green",
  recovered: "green", silenced: "muted", maintenance: "muted" };
const STATUS_WORD: Record<StatusKey, string> = { firing: "Firing", acknowledged: "Acknowledged", pending: "Pending", resolved: "Resolved",
  recovered: "Recovered", silenced: "Silenced", maintenance: "Maintenance" };
function parseStatus(w: unknown, fallback: StatusKey): Status {
  const o = obj(w);
  const key = (STATUS_KEYS.has(str(o.key)) ? str(o.key) : fallback) as StatusKey;
  const t = str(o.tone);
  const tone = (t === "red" || t === "amber" || t === "grey" || t === "green" || t === "muted" ? t : STATUS_TONE[key]) as Status["tone"];
  return { key, word: str(o.word, STATUS_WORD[key]), tone, tip: optStr(o.tip) };
}

export type RuleFacts = { expr: string; intervalS: number | null; window: string | null; forS: number; keepFiringForS: number;
  threshold: string | null; minRequests: number | null; routing: string | null; routingTip: string | null; datasource: string | null };

export type RuleKind = "error_rate" | "latency_p95" | "health_check" | "no_traffic" | "deploy_health" | "custom";
export type AlertRule = {
  id: string; kind: RuleKind; label: string; alert: string; expr: string; forSeconds: number; condition: string;
  params: Record<string, number>; bounds: Record<string, [number, number]>; severity: Sev;
  sample: boolean; facts: RuleFacts;
  summary: string | null; description: string | null; runbook: Runbook;
  enabled: boolean; triage: boolean; autoRespond: boolean; respondOnBehalfOf: string | null; notify: string;
  source: string; sourceWord: string; changedBy: string | null; changedAt: string | null;
  /** "code": declared in the repo's boss.deploy.yaml (repo-managed, D4). */
  managedBy: string;
  /** The latest deploy's boss.deploy.yaml no longer declares it; BOSS keeps it running. */
  droppedWord: string | null;
  /** Tune: a repo rule is proposed as a pull request to its manifest; any other is saved here. */
  tuneVia: "pull_request" | "console";
  /** Where a repo rule lives: the manifest's path and the lines of its rule. */
  where: { path: string; line: number; endLine: number; href: string | null } | null;
  state: string | null; stateWord: string; stateTone: Tone; stateTip: string | null;
  value: number | null; valueWord: string | null; evaluatedAt: string | null; evaluatedBy: string | null;
  silence: { id: string; endsAt: string; reason: string; by: string | null } | null;
  editable: "params" | "promql";
};
const KINDS = new Set(["error_rate", "latency_p95", "health_check", "no_traffic", "deploy_health", "custom"]);
export function parseRule(w: unknown): AlertRule {
  const o = obj(w);
  const params: Record<string, number> = {};
  for (const [k, v] of Object.entries(obj(o.params))) if (typeof v === "number") params[k] = v;
  const bounds: Record<string, [number, number]> = {};
  for (const [k, v] of Object.entries(obj(o.bounds))) { const a = arr(v); bounds[k] = [num(a[0]), num(a[1])]; }
  const s = o.silence ? obj(o.silence) : null;
  const f = obj(o.facts);
  const forS = typeof f.for_s === "number" ? f.for_s : num(o.for_seconds);
  return {
    id: str(o.id), kind: (KINDS.has(str(o.kind)) ? str(o.kind) : "custom") as RuleKind, label: str(o.label, str(o.alert)),
    alert: str(o.alert), expr: str(o.expr), forSeconds: num(o.for_seconds), condition: str(o.condition),
    params, bounds, severity: sevOf(o.sev, o.severity), sample: o.sample === true,
    facts: { expr: str(f.expr, str(o.expr)), intervalS: optNum(f.interval_s), window: optStr(f.window), forS,
             keepFiringForS: typeof f.keep_firing_for_s === "number" ? f.keep_firing_for_s : num(o.keep_firing_for_s),
             threshold: optStr(f.threshold), minRequests: optNum(f.min_requests), routing: optStr(f.routing) ?? optStr(o.notify),
             routingTip: optStr(f.routing_tip), datasource: optStr(f.datasource) ?? optStr(o.evaluated_by) },
    summary: optStr(o.summary), description: optStr(o.description), runbook: parseRunbook(o.runbook),
    enabled: o.enabled !== false, triage: o.triage !== false, autoRespond: o.auto_respond === true,
    respondOnBehalfOf: optStr(o.respond_on_behalf_of), notify: str(o.notify, "console"),
    source: str(o.source), sourceWord: str(o.source_word), changedBy: optStr(o.changed_by), changedAt: optStr(o.changed_at),
    managedBy: str(o.managed_by, str(o.source) === "yaml" ? "code" : "builtin"),
    droppedWord: optStr(o.dropped_word) ?? null,
    tuneVia: o.tune_via === "pull_request" ? "pull_request" : "console",
    where: o.where && typeof o.where === "object" && str(obj(o.where).path)
      ? { path: str(obj(o.where).path), line: num(obj(o.where).line), endLine: num(obj(o.where).end_line, num(obj(o.where).line)),
          href: optStr(obj(o.where).href) } : null,
    state: optStr(o.state), stateWord: str(o.state_word, "Not evaluated yet"), stateTone: toneOf(o.state_tone),
    stateTip: optStr(o.state_tip), value: optNum(o.value), valueWord: optStr(o.value_word),
    evaluatedAt: optStr(o.evaluated_at), evaluatedBy: optStr(o.evaluated_by),
    silence: s ? { id: str(s.id), endsAt: str(s.ends_at), reason: str(s.reason), by: optStr(s.by) } : null,
    editable: o.editable === "promql" ? "promql" : "params",
  };
}

export type SignalSource = { id: string; app: string; kind: string; connected: boolean; lastReportAt: string | null;
  reportsToday: number; lastPollAt: string | null; lastWord: string | null;
  token: { minted: boolean; mintedAt: string | null; rotatedAt: string | null } };
const parseSource = (w: unknown): SignalSource => {
  const o = obj(w);
  const t = obj(o.token);
  return { id: str(o.id), app: str(o.app), kind: str(o.kind, "signed reports"), connected: o.connected === true,
           lastReportAt: optStr(o.last_report_at), reportsToday: num(o.reports_today), lastPollAt: optStr(o.last_poll_at),
           lastWord: optStr(o.last_word),
           token: { minted: t.minted === true, mintedAt: optStr(t.minted_at), rotatedAt: optStr(t.rotated_at) } };
};

export type Triage = { state: string; word: string; outcome: string | null; reason: string | null; question: string | null;
  by: string | null; at: string | null; confidence: number | null;
  /** The agent that decided (its role), apart from the model it thought with. */
  agent: { roleKey: string; role: string } | null; model: string | null;
  /** While the agent asks: the options a person answers with (one button each). */
  options: TriageOption[];
  /** A person's answer to the agent's question: who, when, which option. */
  answered: { name: string; at: string | null; action: string } | null };
export type TriageOption = { action: string; label: string };
export const parseTriageOptions = (v: unknown): TriageOption[] =>
  arr(v).map((x) => { const o = obj(x); return { action: str(o.action), label: str(o.label) || str(o.action) }; }).filter((o) => o.action);
export type Alert = {
  id: string; targetId: string; app: string; env: string; ruleId: string | null; kind: string; label: string; alertname: string;
  summary: string; description: string | null; severity: Sev; state: "firing" | "pending" | "resolved";
  status: Status; open: boolean; needsAttention: boolean; durationS: number | null;
  stateWord: string; activeAt: string; firedAt: string | null; clearedAt: string | null; clearedWord: string | null;
  fireCount: number; value: number | null; valueWord: string; thresholdWord: string | null; expr: string | null;
  evaluatedBy: string | null; release: { sha7: string | null; ref: string | null } | null;
  incident: { id: string; title: string; status: string; word: string } | null;
  heldBack: { by: string; word: string } | null; triage: Triage; response: { runId: string | null; word: string } | null;
  runbook: Runbook; exploreUrl: string | null; exploreReason: string | null; sourceKind: "rule" | "signal";
  reporter: { targetId: string | null; app: string | null } | null; backfilled: boolean;
  /** Phase 1a (monitoring): where it came from, how loud it is, and what holds it back. */
  subjectId: string | null; serviceId: string | null; source: string | null; urgency: "high" | "low";
  flapping: boolean; flapScore: number | null; flapTuning: { status: string; action: string; reason: string } | null;
  acked: { by: string; at: string; until: string | null } | null; noDataSince: string | null; keepFiringSince: string | null;
  suppressed: { kind: string; id: string; until: string | null; word: string; reason: string | null }[];
  links: { href: string; text: string }[]; notifiedAt: string | null;
};
const STATES = new Set(["firing", "pending", "resolved"]);
function fallbackStatus(state: Alert["state"], acked: boolean, held: boolean): StatusKey {
  if (state === "resolved") return "resolved";
  if (state === "pending") return "pending";
  return held ? "silenced" : acked ? "acknowledged" : "firing";
}
export function parseAlert(w: unknown): Alert {
  const o = obj(w);
  const t = obj(o.triage);
  const rel = o.release ? obj(o.release) : null;
  const inc = o.incident ? obj(o.incident) : null;
  const held = o.held_back ? obj(o.held_back) : null;
  const resp = o.response ? obj(o.response) : null;
  const rep = o.reporter ? obj(o.reporter) : null;
  const ft = o.flap_tuning ? obj(o.flap_tuning) : null;
  const ack = o.acked ? obj(o.acked) : null;
  const state = (STATES.has(str(o.state)) ? str(o.state) : "resolved") as Alert["state"];
  return {
    id: str(o.id), targetId: str(o.target_id), app: str(o.app), env: str(o.env), ruleId: optStr(o.rule_id), kind: str(o.kind),
    label: str(o.label), alertname: str(o.alertname), summary: str(o.summary, str(o.alertname)), description: optStr(o.description),
    severity: sevOf(o.sev, o.severity),
    state, stateWord: str(o.state_word),
    status: parseStatus(o.status, fallbackStatus(state, Boolean(ack), Boolean(held) || arr(o.suppressed).length > 0)),
    open: typeof o.open === "boolean" ? o.open : state !== "resolved",
    needsAttention: typeof o.needs_attention === "boolean" ? o.needs_attention : state === "firing" && !ack && !held && arr(o.suppressed).length === 0,
    durationS: optNum(o.duration_s),
    activeAt: str(o.active_at), firedAt: optStr(o.fired_at), clearedAt: optStr(o.cleared_at), clearedWord: optStr(o.cleared_word),
    fireCount: num(o.fire_count, 1), value: optNum(o.value), valueWord: str(o.value_word, "—"), thresholdWord: optStr(o.threshold_word),
    expr: optStr(o.expr), evaluatedBy: optStr(o.evaluated_by),
    release: rel ? { sha7: optStr(rel.sha7), ref: optStr(rel.ref) } : null,
    incident: inc ? { id: str(inc.id), title: str(inc.title), status: str(inc.status), word: str(inc.word) } : null,
    heldBack: held ? { by: str(held.by), word: str(held.word) } : null,
    triage: { state: str(t.state, "none"), word: str(t.word, "Not triaged"), outcome: optStr(t.outcome), reason: optStr(t.reason),
              question: optStr(t.question), by: optStr(t.by), at: optStr(t.at), confidence: optNum(t.confidence),
              agent: t.agent && typeof t.agent === "object" && str(obj(t.agent).role_key)
                ? { roleKey: str(obj(t.agent).role_key), role: str(obj(t.agent).role) } : null,
              model: optStr(t.model), options: parseTriageOptions(t.options),
              answered: t.answered && typeof t.answered === "object"
                ? { name: str(obj(t.answered).name, "A person"), at: optStr(obj(t.answered).at), action: str(obj(t.answered).action) } : null },
    response: resp ? { runId: optStr(resp.run_id), word: str(resp.word) } : null,
    runbook: parseRunbook(o.runbook), exploreUrl: optStr(o.explore_url), exploreReason: optStr(o.explore_reason),
    sourceKind: o.source_kind === "signal" ? "signal" : "rule",
    reporter: rep ? { targetId: optStr(rep.target_id), app: optStr(rep.app) } : null, backfilled: o.backfilled === true,
    subjectId: optStr(o.subject_id), serviceId: optStr(o.service_id), source: optStr(o.source),
    urgency: o.urgency === "low" ? "low" : "high", flapping: o.flapping === true, flapScore: optNum(o.flap_score),
    flapTuning: ft ? { status: str(ft.status), action: str(ft.action), reason: str(ft.reason) } : null,
    acked: ack ? { by: str(ack.by), at: str(ack.at), until: optStr(ack.until) } : null,
    noDataSince: optStr(o.no_data_since), keepFiringSince: optStr(o.keep_firing_since),
    suppressed: arr(o.suppressed).map((x) => { const y = obj(x); return { kind: str(y.kind), id: str(y.id), until: optStr(y.until),
                                                                          word: str(y.word), reason: optStr(y.reason) }; }),
    links: arr(o.links).map((x) => { const y = obj(x); return { href: str(y.href), text: str(y.text, str(y.href)) }; })
      .filter((l) => /^https?:\/\//.test(l.href)),
    notifiedAt: optStr(o.notified_at),
  };
}

export type RulesView = {
  targetId: string; app: string; env: string; rules: AlertRule[]; sources: SignalSource[];
  changes: { ruleId: string; change: string; reason: string | null; by: string | null; at: string | null }[];
  evaluator: string; evaluatorTip: string | null; severities: SevInfo[]; hasSamples: boolean;
};
export function parseRulesView(w: unknown): RulesView {
  const o = obj(w);
  return {
    targetId: str(o.target_id), app: str(o.app), env: str(o.env), rules: arr(o.rules).map(parseRule),
    sources: arr(o.sources).map(parseSource),
    changes: arr(o.changes).map((c) => { const x = obj(c); return { ruleId: str(x.rule_id), change: str(x.change), reason: optStr(x.reason),
                                                                    by: optStr(x.by), at: optStr(x.at) }; }),
    evaluator: str(o.evaluator, "BOSS's own monitoring"), evaluatorTip: optStr(o.evaluator_tip),
    severities: parseSeverities(o.severities),
    hasSamples: typeof o.has_samples === "boolean" ? o.has_samples : arr(o.rules).some((r) => obj(r).sample === true),
  };
}

/** An open incident, worded from its alerts ("Firing: 2 alerts", "Recovered at 22:46; …"). */
export type OpenIncident = {
  id: string; title: string; status: string; lifecycle: string; since: string | null; firing: number; happening: boolean;
  subjectId: string | null; urgency: "high" | "low"; acked: boolean; responseRunId: string | null; responseStatus: string | null;
  word: string; sev: Sev; chip: Status; ackedBy: string | null;
  /** When it recovered (nothing firing), for the viewer's own clock. */
  recoveredAt: string | null;
};
export type AlertsView = {
  alerts: Alert[]; counts: { firing: number; pending: number; needsAttention: number }; severities: SevInfo[];
  apps: { targetId: string; app: string; env: string; service: boolean }[];
  grafana: { state: string; reason: string | null };
  incidents: OpenIncident[];
};
export function parseAlertsView(w: unknown): AlertsView {
  const o = obj(w);
  const c = obj(o.counts);
  const g = obj(o.grafana);
  const alerts = arr(o.alerts).map(parseAlert);
  return {
    alerts, counts: { firing: num(c.firing), pending: num(c.pending),
                      needsAttention: typeof c.needs_attention === "number" ? c.needs_attention : alerts.filter((a) => a.needsAttention).length },
    severities: parseSeverities(o.severities),
    apps: arr(o.apps).map((a) => { const x = obj(a); return { targetId: str(x.target_id), app: str(x.app), env: str(x.env), service: x.service === true }; }),
    grafana: { state: str(g.state, "unchecked"), reason: optStr(g.reason) },
    incidents: arr(o.incidents).map((w) => {
      const x = obj(w);
      return { id: str(x.id), title: str(x.title), status: str(x.status), lifecycle: str(x.lifecycle), since: optStr(x.since),
               firing: num(x.firing), happening: x.happening === true, subjectId: optStr(x.subject_id),
               urgency: x.urgency === "low" ? "low" : "high", acked: Boolean(x.acked), responseRunId: optStr(x.response_run_id),
               responseStatus: optStr(x.response_status), word: str(x.word), sev: sevOf(x.sev, x.severity),
               chip: parseStatus(x.status_chip, x.happening === true ? (x.acked ? "acknowledged" : "firing") : "recovered"),
               ackedBy: optStr(x.acked_by) ?? (typeof x.acked === "string" ? x.acked : null),
               recoveredAt: optStr(x.recovered_at) } as OpenIncident;
    }),
  };
}

/** Grafana as the console may show it: a link only in state `answering`, else the reason. */
export type Grafana = { state: "answering" | "not_answering" | "unchecked" | "not_deployed"; url: string | null;
  reason: string | null; checkedAt: string | null; why: string | null };
const G_STATES = new Set(["answering", "not_answering", "unchecked", "not_deployed"]);
export function parseGrafana(w: unknown): Grafana {
  const o = obj(w);
  const state = (G_STATES.has(str(o.state)) ? str(o.state) : "unchecked") as Grafana["state"];
  return { state, url: state === "answering" ? optStr(o.url) : null, reason: optStr(o.reason), checkedAt: optStr(o.checked_at),
           why: optStr(o.why) };
}

// ── urls + hooks ────────────────────────────────────────────────────────────────────────────

const q = (projectId: string, extra: Record<string, string | null | undefined> = {}) => {
  const p = new URLSearchParams({ project_id: projectId });
  for (const [k, v] of Object.entries(extra)) if (v) p.set(k, v);
  return p.toString();
};
const appPath = (targetId: string) => `/v1/apps/${encodeURIComponent(targetId)}`;
export const rulesUrl = (projectId: string, targetId: string) => `${appPath(targetId)}/alert-rules?${q(projectId)}`;
export type HistoryWin = "firing" | "24h" | "7d";
export const HISTORY_WINS: { key: HistoryWin; label: string; tip: string }[] = [
  { key: "firing", label: "Firing", tip: "Alerts firing now" },
  { key: "24h", label: "24 h", tip: "Every alert in the last 24 hours" },
  { key: "7d", label: "7 days", tip: "Every alert in the last 7 days" },
];
export const historyUrl = (projectId: string, targetId: string, win: HistoryWin) =>
  `${appPath(targetId)}/alerts?${q(projectId, win === "firing" ? { state: "firing" } : { state: "all", since: win })}`;
export type AlertSource = "rule" | "external" | "synthetic" | "heartbeat" | "signal";
export const ALERT_SOURCES: { key: AlertSource; label: string; tip: string }[] = [
  { key: "rule", label: "BOSS rules", tip: "Rules BOSS evaluates" },
  { key: "external", label: "Other tools", tip: "Alertmanager, Grafana, Sentry, Datadog, CloudWatch, PagerDuty-style" },
  { key: "synthetic", label: "URL checks", tip: "BOSS calling a URL on a schedule" },
  { key: "heartbeat", label: "Heartbeats", tip: "A job that stopped checking in" },
  { key: "signal", label: "Signals", tip: "Signed reports from an app" },
];
export type AlertsFilter = { app: string | null; state: "firing" | "resolved" | "all"; severity: Sev | null;
  source?: AlertSource | null; flapping?: boolean | null; acked?: boolean | null; attention?: boolean | null };
const tri = (v: boolean | null | undefined) => (v === true ? "true" : v === false ? "false" : null);
export const allAlertsUrl = (projectId: string, f: AlertsFilter) =>
  `/v1/alerts?${q(projectId, { app: f.app, state: f.state, severity: f.severity, since: f.state === "firing" ? null : "7d",
                               source: f.source ?? null, flapping: tri(f.flapping), acked: tri(f.acked),
                               attention: f.attention ? "true" : null })}`;
export const ACK_PATH = "/v1/alerts/ack";
export const rulesPutPath = (targetId: string) => `${appPath(targetId)}/alert-rules`;
export const tunePreviewPath = (targetId: string, ruleId: string) => `${appPath(targetId)}/alert-rules/${encodeURIComponent(ruleId)}/tune-preview`;
export const proposePath = (targetId: string, ruleId: string) => `${appPath(targetId)}/alert-rules/${encodeURIComponent(ruleId)}/propose`;
/** Tune's tooltip: where the change goes. */
export const tuneTip = (r: Pick<AlertRule, "tuneVia" | "where">) => r.tuneVia === "pull_request"
  ? `Change its threshold: proposes a pull request to ${r.where ? whereWord(r.where) : "boss.deploy.yaml"}`
  : "Change its threshold: saved here, with who and why";
/** "boss.deploy.yaml:12" — where a repo rule lives. */
export const whereWord = (w: NonNullable<AlertRule["where"]>) => `${w.path}:${w.line}`;
export const silencePath = (targetId: string, ruleId: string) => `${appPath(targetId)}/alert-rules/${encodeURIComponent(ruleId)}/silence`;
export const endSilencePath = (targetId: string, silenceId: string) => `${appPath(targetId)}/alert-silences/${encodeURIComponent(silenceId)}/end`;
export const triagePath = (targetId: string, alertId: string) => `${appPath(targetId)}/alerts/${encodeURIComponent(alertId)}/triage`;
/** A person answers the agent's question (POST {project_id, actor, action}). */
export const answerPath = (targetId: string, alertId: string) => `${triagePath(targetId, alertId)}/answer`;
export const rotatePath = (targetId: string, sourceId: string) => `${appPath(targetId)}/signal-sources/${encodeURIComponent(sourceId)}/rotate`;
export const GRAFANA_CHECK_PATH = "/v1/observability/grafana/check";
export const removeSamplesPath = (targetId: string) => `${appPath(targetId)}/alert-rules/samples/remove`;
export const PREVIEW_PATH = "/v1/alert-rules/preview";

// ── the rule builder: an app's actual series ────────────────────────────────────────────────
export type MetricType = "counter" | "gauge" | "histogram" | "summary" | "unknown";
export type MetricName = { name: string; type: MetricType; labels: string[]; help: string | null };
export type MetricNames = { selector: string; metrics: MetricName[]; word: string | null; evaluator: string | null };
const M_TYPES = new Set(["counter", "gauge", "histogram", "summary"]);
export function parseMetricNames(w: unknown): MetricNames {
  const o = obj(w);
  return {
    selector: str(o.selector), word: optStr(o.word), evaluator: optStr(o.evaluator),
    metrics: arr(o.metrics).map((x) => { const m = obj(x); return { name: str(m.name), type: (M_TYPES.has(str(m.type)) ? str(m.type) : "unknown") as MetricType,
                                                                    labels: arr(m.labels).map((l) => str(l)).filter(Boolean), help: optStr(m.help) }; })
      .filter((m) => m.name),
  };
}
export const metricNamesUrl = (projectId: string, targetId: string) => `${appPath(targetId)}/metric-names?${q(projectId)}`;
export const useMetricNames = (projectId: string, targetId: string | null): Polled<MetricNames> =>
  usePolled(projectId && targetId ? metricNamesUrl(projectId, targetId) : null, parseMetricNames, () => 300_000);

export const ALERTS_POLL_MS = 30_000;
export const useRules = (projectId: string, targetId: string | null): Polled<RulesView> =>
  usePolled(projectId && targetId ? rulesUrl(projectId, targetId) : null, parseRulesView, () => ALERTS_POLL_MS);
export const useHistory = (projectId: string, targetId: string | null, win: HistoryWin): Polled<AlertsView> =>
  usePolled(projectId && targetId ? historyUrl(projectId, targetId, win) : null, parseAlertsView, () => ALERTS_POLL_MS);
export const useAllAlerts = (projectId: string, f: AlertsFilter): Polled<AlertsView> =>
  usePolled(projectId ? allAlertsUrl(projectId, f) : null, parseAlertsView, () => ALERTS_POLL_MS);

/** `5m`-style durations the edit sheet writes for a rule's `for`. */
export function forWord(seconds: number): string {
  if (!seconds) return "0m";
  return seconds % 60 === 0 ? `${seconds / 60}m` : `${seconds}s`;
}

/** "45 s", "6 min", "1 h 12 min", "3 d 4 h": how long an alert was (or has been) open. */
export function durationWord(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d} d ${h % 24} h` : `${d} d`;
}

/** An alert's open time, from the wire's duration or its own timestamps. */
export function alertDuration(a: Pick<Alert, "durationS" | "firedAt" | "activeAt" | "clearedAt"> & { state?: Alert["state"] }, now = Date.now()): number | null {
  if (a.durationS !== null) return a.durationS;
  // Pending counts from when it went pending (a re-pended alert keeps its first fired_at).
  const start = Date.parse(a.state === "pending" ? a.activeAt : a.firedAt ?? a.activeAt);
  if (!Number.isFinite(start)) return null;
  const end = a.clearedAt ? Date.parse(a.clearedAt) : now;
  return Number.isFinite(end) ? Math.max(0, (end - start) / 1000) : null;
}
