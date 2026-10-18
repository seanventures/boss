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

export type RuleKind = "error_rate" | "latency_p95" | "health_check" | "no_traffic" | "deploy_health" | "custom";
export type AlertRule = {
  id: string; kind: RuleKind; label: string; alert: string; expr: string; forSeconds: number; condition: string;
  params: Record<string, number>; bounds: Record<string, [number, number]>; severity: "warning" | "critical";
  summary: string | null; description: string | null; runbook: Runbook;
  enabled: boolean; triage: boolean; autoRespond: boolean; respondOnBehalfOf: string | null; notify: string;
  source: string; sourceWord: string; changedBy: string | null; changedAt: string | null;
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
  return {
    id: str(o.id), kind: (KINDS.has(str(o.kind)) ? str(o.kind) : "custom") as RuleKind, label: str(o.label, str(o.alert)),
    alert: str(o.alert), expr: str(o.expr), forSeconds: num(o.for_seconds), condition: str(o.condition),
    params, bounds, severity: o.severity === "critical" ? "critical" : "warning",
    summary: optStr(o.summary), description: optStr(o.description), runbook: parseRunbook(o.runbook),
    enabled: o.enabled !== false, triage: o.triage !== false, autoRespond: o.auto_respond === true,
    respondOnBehalfOf: optStr(o.respond_on_behalf_of), notify: str(o.notify, "console"),
    source: str(o.source), sourceWord: str(o.source_word), changedBy: optStr(o.changed_by), changedAt: optStr(o.changed_at),
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
  by: string | null; at: string | null; confidence: number | null };
export type Alert = {
  id: string; targetId: string; app: string; env: string; ruleId: string | null; kind: string; label: string; alertname: string;
  summary: string; description: string | null; severity: "warning" | "critical"; state: "firing" | "pending" | "resolved";
  stateWord: string; activeAt: string; firedAt: string | null; clearedAt: string | null; clearedWord: string | null;
  fireCount: number; value: number | null; valueWord: string; thresholdWord: string | null; expr: string | null;
  evaluatedBy: string | null; release: { sha7: string | null; ref: string | null } | null;
  incident: { id: string; title: string; status: string; word: string } | null;
  heldBack: { by: string; word: string } | null; triage: Triage; response: { runId: string | null; word: string } | null;
  runbook: Runbook; exploreUrl: string | null; exploreReason: string | null; sourceKind: "rule" | "signal";
  reporter: { targetId: string | null; app: string | null } | null; backfilled: boolean;
};
const STATES = new Set(["firing", "pending", "resolved"]);
export function parseAlert(w: unknown): Alert {
  const o = obj(w);
  const t = obj(o.triage);
  const rel = o.release ? obj(o.release) : null;
  const inc = o.incident ? obj(o.incident) : null;
  const held = o.held_back ? obj(o.held_back) : null;
  const resp = o.response ? obj(o.response) : null;
  const rep = o.reporter ? obj(o.reporter) : null;
  return {
    id: str(o.id), targetId: str(o.target_id), app: str(o.app), env: str(o.env), ruleId: optStr(o.rule_id), kind: str(o.kind),
    label: str(o.label), alertname: str(o.alertname), summary: str(o.summary, str(o.alertname)), description: optStr(o.description),
    severity: o.severity === "critical" ? "critical" : "warning",
    state: (STATES.has(str(o.state)) ? str(o.state) : "resolved") as Alert["state"], stateWord: str(o.state_word),
    activeAt: str(o.active_at), firedAt: optStr(o.fired_at), clearedAt: optStr(o.cleared_at), clearedWord: optStr(o.cleared_word),
    fireCount: num(o.fire_count, 1), value: optNum(o.value), valueWord: str(o.value_word, "—"), thresholdWord: optStr(o.threshold_word),
    expr: optStr(o.expr), evaluatedBy: optStr(o.evaluated_by),
    release: rel ? { sha7: optStr(rel.sha7), ref: optStr(rel.ref) } : null,
    incident: inc ? { id: str(inc.id), title: str(inc.title), status: str(inc.status), word: str(inc.word) } : null,
    heldBack: held ? { by: str(held.by), word: str(held.word) } : null,
    triage: { state: str(t.state, "none"), word: str(t.word, "Not triaged"), outcome: optStr(t.outcome), reason: optStr(t.reason),
              question: optStr(t.question), by: optStr(t.by), at: optStr(t.at), confidence: optNum(t.confidence) },
    response: resp ? { runId: optStr(resp.run_id), word: str(resp.word) } : null,
    runbook: parseRunbook(o.runbook), exploreUrl: optStr(o.explore_url), exploreReason: optStr(o.explore_reason),
    sourceKind: o.source_kind === "signal" ? "signal" : "rule",
    reporter: rep ? { targetId: optStr(rep.target_id), app: optStr(rep.app) } : null, backfilled: o.backfilled === true,
  };
}

export type RulesView = {
  targetId: string; app: string; env: string; rules: AlertRule[]; sources: SignalSource[];
  changes: { ruleId: string; change: string; reason: string | null; by: string | null; at: string | null }[];
  evaluator: string; evaluatorTip: string | null;
};
export function parseRulesView(w: unknown): RulesView {
  const o = obj(w);
  return {
    targetId: str(o.target_id), app: str(o.app), env: str(o.env), rules: arr(o.rules).map(parseRule),
    sources: arr(o.sources).map(parseSource),
    changes: arr(o.changes).map((c) => { const x = obj(c); return { ruleId: str(x.rule_id), change: str(x.change), reason: optStr(x.reason),
                                                                    by: optStr(x.by), at: optStr(x.at) }; }),
    evaluator: str(o.evaluator, "BOSS's own monitoring"), evaluatorTip: optStr(o.evaluator_tip),
  };
}

export type AlertsView = {
  alerts: Alert[]; counts: { firing: number; pending: number };
  apps: { targetId: string; app: string; env: string }[];
  grafana: { state: string; reason: string | null };
};
export function parseAlertsView(w: unknown): AlertsView {
  const o = obj(w);
  const c = obj(o.counts);
  const g = obj(o.grafana);
  return {
    alerts: arr(o.alerts).map(parseAlert), counts: { firing: num(c.firing), pending: num(c.pending) },
    apps: arr(o.apps).map((a) => { const x = obj(a); return { targetId: str(x.target_id), app: str(x.app), env: str(x.env) }; }),
    grafana: { state: str(g.state, "unchecked"), reason: optStr(g.reason) },
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
export type AlertsFilter = { app: string | null; state: "firing" | "resolved" | "all"; severity: "warning" | "critical" | null };
export const allAlertsUrl = (projectId: string, f: AlertsFilter) =>
  `/v1/alerts?${q(projectId, { app: f.app, state: f.state, severity: f.severity, since: f.state === "firing" ? null : "7d" })}`;
export const rulesPutPath = (targetId: string) => `${appPath(targetId)}/alert-rules`;
export const silencePath = (targetId: string, ruleId: string) => `${appPath(targetId)}/alert-rules/${encodeURIComponent(ruleId)}/silence`;
export const endSilencePath = (targetId: string, silenceId: string) => `${appPath(targetId)}/alert-silences/${encodeURIComponent(silenceId)}/end`;
export const triagePath = (targetId: string, alertId: string) => `${appPath(targetId)}/alerts/${encodeURIComponent(alertId)}/triage`;
export const rotatePath = (targetId: string, sourceId: string) => `${appPath(targetId)}/signal-sources/${encodeURIComponent(sourceId)}/rotate`;
export const GRAFANA_CHECK_PATH = "/v1/observability/grafana/check";

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
