/**
 * App alerts fixtures: WIRE bodies (snake_case, exactly engine/boss_engine/services/
 * app_alerts_view.py's shapes) for an app's rules, its history and every app's alerts.
 *
 * t_down (api · prod) has the D2-shaped story: its error-rate rule is firing (14.4%, release
 * 6bf2673, the agent opened an incident, Respond automatically on), HelpDesk AI reported it, and
 * yesterday's latency breach was noise.
 */
/** obs-fixtures' clock (not imported: obs-fixtures routes to this module). */
export const NOW = "2026-10-04T12:00:00.000Z";
const NOW_MS = Date.parse(NOW);

const iso = (ms: number) => new Date(ms).toISOString();
const MIN = 60_000;
const SEL = 'app="api",environment="prod"';
export const ERROR_EXPR = `100 * sum(increase(http_requests_total{${SEL},status=~"5.."}[5m])) / sum(increase(http_requests_total{${SEL}}[5m])) > 5 and sum(increase(http_requests_total{${SEL}}[5m])) >= 20`;
const RUNBOOK = { url: "boss:runbook/app-serving", kind: "builtin", title: "The app is serving badly",
                  markdown: "# The app is serving badly\n\n1. **Which release is serving?**", href: null };

export function ruleWire(kind: string, over: Record<string, unknown> = {}) {
  const base: Record<string, Record<string, unknown>> = {
    error_rate: { label: "Error rate", alert: "AppErrorRate", condition: "> 5% for 5 min (≥ 20 req)", severity: "critical",
                  params: { threshold: 5, window_minutes: 5, min_requests: 20 }, state: "firing", state_word: "Firing", state_tone: "red",
                  value: 14.4, value_word: "14.4%", auto_respond: true, respond_on_behalf_of: "Param", source: "console",
                  source_word: "changed in the console", changed_by: "Param", changed_at: iso(NOW_MS - 3 * 3600_000) },
    latency_p95: { label: "p95 latency", alert: "AppLatencyP95", condition: "> 800 ms for 5 min", severity: "warning",
                   params: { threshold: 800, window_minutes: 5, min_requests: 20 }, state: "ok", state_word: "OK", state_tone: "green",
                   value: 212, value_word: "212 ms", source: "yaml", source_word: "from boss.deploy.yaml" },
    health_check: { label: "Health check", alert: "AppHealthCheckFailing", condition: "3 failed probes in 10 min", severity: "critical",
                    params: { min_failures: 3, window_minutes: 10 }, state: "ok", state_word: "OK", state_tone: "green",
                    source: "builtin", source_word: "BOSS default" },
    no_traffic: { label: "No traffic", alert: "AppNoTraffic", condition: "0 req for 15 min", severity: "warning", enabled: false,
                  params: { window_minutes: 15, expected_hours: 2 }, state: "off", state_word: "Off", state_tone: "grey",
                  source: "builtin", source_word: "BOSS default" },
    deploy_health: { label: "Deploy health", alert: "AppDeployUnhealthy", condition: "newest deploy failed its health check",
                     severity: "critical", params: {}, state: "ok", state_word: "OK", state_tone: "green", source: "builtin",
                     source_word: "BOSS default" },
  };
  const b = base[kind];
  return {
    id: `arule_${kind}`, kind, expr: kind === "error_rate" ? ERROR_EXPR : `x{${SEL}}`, for_seconds: 0, labels: { severity: b.severity },
    bounds: { threshold: [0.1, 100], window_minutes: [1, 1440], min_requests: [1, 1_000_000], min_failures: [1, 1000], expected_hours: [1, 168] },
    summary: `${b.label} on api`, description: b.condition, runbook: RUNBOOK, enabled: true, triage: true, auto_respond: false,
    respond_on_behalf_of: null, notify: "console", changed_by: null, changed_at: null, state_tip: null, value: null, value_word: null,
    evaluated_at: NOW, evaluated_by: "BOSS's own monitoring", silence: null, editable: "params",
    sev: b.severity === "critical" ? "sev1" : "sev3", sample: false,
    facts: { expr: kind === "error_rate" ? ERROR_EXPR : `x{${SEL}}`, interval_s: 60, window: kind === "deploy_health" ? null : "5m",
             for_s: 0, keep_firing_for_s: 0, threshold: kind === "error_rate" ? "> 5" : null,
             min_requests: kind === "error_rate" || kind === "latency_p95" ? 20 : null, routing: "Console · On-call email",
             routing_tip: "Default policy: console, then email to the owner", datasource: "BOSS's own records" },
    ...b, ...over,
  };
}

export function rulesWire(targetId = "t_down", over: Record<string, unknown> = {}) {
  return {
    target_id: targetId, app: "api", env: "prod",
    rules: ["error_rate", "latency_p95", "health_check", "no_traffic", "deploy_health"].map((k) => ruleWire(k)),
    sources: [{ id: "t_desk", app: "HelpDesk AI", kind: "signed reports", connected: true, last_report_at: iso(NOW_MS - 65 * MIN),
                reports_today: 7, last_poll_at: iso(NOW_MS - MIN), last_word: "No signals",
                token: { minted: true, minted_at: iso(NOW_MS - 30 * 24 * 3600_000), rotated_at: null } }],
    changes: [{ rule_id: "arule_error_rate", change: "edited", reason: "checkout matters", by: "Param", at: iso(NOW_MS - 3 * 3600_000) }],
    evaluator: "BOSS's own monitoring", evaluator_tip: "Prometheus isn't answering on its host", has_samples: false,
    export: { groups: [] }, grafana: { state: "answering", reason: null }, etag: `"rules-${targetId}"`,
    ...over,
  };
}

export const FIRING_ALERT = {
  id: "alrt_err", target_id: "t_down", app: "api", env: "prod", rule_id: "arule_error_rate", kind: "error_rate", label: "Error rate",
  alertname: "AppErrorRate", summary: "Error rate on api", description: "> 5% for 5 min (≥ 20 req)", severity: "critical", state: "firing",
  state_word: "Firing", active_at: iso(NOW_MS - 12 * MIN), fired_at: iso(NOW_MS - 12 * MIN), cleared_at: null, cleared_word: null,
  fire_count: 2, value: 14.4, value_word: "14.4%", threshold: 5, threshold_word: "5%", expr: ERROR_EXPR,
  evaluated_by: "BOSS's own monitoring", release: { sha7: "6bf2673", ref: "6bf26734d875", deployed_at: iso(NOW_MS - 13 * MIN) },
  incident: { id: "inc_1", title: "api (prod): 14.4% of requests failing", status: "triaged", word: "Triaged" },
  held_back: null,
  triage: { state: "done", word: "Opened an incident", outcome: "opened",
            reason: "App-side: release 6bf2673 went out a minute before; POST /orders fails 14.4% of the time.",
            question: null, by: "claude-sonnet-4-5", at: iso(NOW_MS - 11 * MIN), confidence: 0.92 },
  response: { run_id: "wfr_1", word: "Responding" }, runbook: RUNBOOK,
  explore_url: "http://grafana.local/explore?schemaVersion=1&orgId=1&panes=x", explore_reason: null,
  source_kind: "rule", reporter: null, backfilled: false,
  sev: "sev1", status: { key: "firing", word: "Firing", tone: "red", tip: "Firing: needs attention" }, open: true,
  needs_attention: true, duration_s: 720,
};
export const SIGNAL_ALERT = {
  ...FIRING_ALERT, id: "alrt_sig", rule_id: null, kind: "signal", label: "Reported", alertname: "AppSignal",
  summary: "HelpDesk AI: 7 checkout complaints", description: null, severity: "warning", value: 7, value_word: "7 reports",
  // No `sev` and no `status`: an older API's answer, read from the severity label (warning → SEV3).
  sev: undefined, status: undefined, duration_s: undefined,
  threshold: null, threshold_word: null, expr: null, fire_count: 1, active_at: iso(NOW_MS - 17 * MIN), fired_at: iso(NOW_MS - 17 * MIN),
  triage: { state: "done", word: "Joined the incident", outcome: "joined", reason: "The same checkout failure, seen by customers.",
            question: null, by: "claude-sonnet-4-5", at: iso(NOW_MS - 16 * MIN), confidence: 0.88 },
  response: null, explore_url: null, explore_reason: null, source_kind: "signal", reporter: { target_id: "t_desk", app: "HelpDesk AI" },
};
export const NOISE_ALERT = {
  ...FIRING_ALERT, id: "alrt_lat", rule_id: "arule_latency_p95", kind: "latency_p95", label: "p95 latency", alertname: "AppLatencyP95",
  summary: "p95 latency on api", severity: "warning", state: "resolved", state_word: "Resolved", value: 1200, value_word: "1.2 s",
  threshold: 800, threshold_word: "800 ms", fire_count: 1, active_at: iso(NOW_MS - 26 * 3600_000), fired_at: iso(NOW_MS - 26 * 3600_000),
  cleared_at: iso(NOW_MS - 26 * 3600_000 + 6 * MIN), cleared_word: "Recovered", incident: null, release: null, response: null,
  triage: { state: "done", word: "Noise", outcome: "noise", reason: "A deploy restart; it cleared on its own in six minutes.",
            question: null, by: "claude-sonnet-4-5", at: iso(NOW_MS - 26 * 3600_000), confidence: 0.9 },
  explore_url: null, explore_reason: "Grafana isn't answering on its host",
  sev: "sev3", status: { key: "resolved", word: "Resolved", tone: "green", tip: "Resolved: it is closed" }, open: false,
  needs_attention: false, duration_s: 360,
};
/** Firing, but someone is on it: amber, with who. */
export const ACKED_ALERT = {
  ...FIRING_ALERT, id: "alrt_ack", sev: "sev2", needs_attention: false,
  status: { key: "acknowledged", word: "Acknowledged · Param", tone: "amber", tip: "Acknowledged: someone is on it" },
  acked: { by: "Param", at: iso(NOW_MS - 5 * MIN), until: iso(NOW_MS + 4 * 3600_000) },
};
export const NO_MODEL_ALERT = {
  ...FIRING_ALERT, id: "alrt_nomodel", incident: null, response: null,
  triage: { state: "no_model", word: "Not triaged: no model is answering", outcome: null, reason: null, question: null, by: null,
            at: NOW, confidence: null },
};

export function historyWire(alerts: unknown[] = [FIRING_ALERT, SIGNAL_ALERT, NOISE_ALERT], over: Record<string, unknown> = {}) {
  return { project_id: "p1", target_id: "t_down", state: "all", since: "24h", severity: null,
           counts: { firing: 2, pending: 0, needs_attention: 1 }, alerts, apps: [{ target_id: "t_down", app: "api", env: "prod" }],
           grafana: { state: "answering", reason: null }, etag: "\"history\"", ...over };
}

export function allAlertsWire(alerts: unknown[] = [FIRING_ALERT, { ...SIGNAL_ALERT }], over: Record<string, unknown> = {}) {
  return historyWire(alerts, { target_id: null, state: "firing", since: "7d",
                               apps: [{ target_id: "t_down", app: "api", env: "prod" }, { target_id: "t_live", app: "invoice", env: "prod" }],
                               ...over });
}
