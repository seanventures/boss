/**
 * Monitoring Phase 1a (BOSSAPP-598): the wire shapes of apps/api/app/routers/monitoring.py.
 *
 *   POST /v1/monitoring/connect · GET /v1/monitoring/services[/{id}/live]      Connect an app
 *   GET  /v1/signal-sources · POST …/{id}/rotate · …/{id}/test                 Sources › SIGNAL SOURCES
 *   GET  /v1/contact-points · POST (add) · …/{id}/test · …/{id}/verify          Notify › CONTACT POINTS
 *   GET  /v1/notification-policies · POST/PUT/DELETE · POST …/preview          Notify › POLICIES, ROUTING
 *   GET  /v1/notifications                                                     Notify › SENT
 *   GET  /v1/silences · POST /v1/silences · …/{id}/end · /v1/maintenance-windows  Notify › SILENCES
 *
 * Every write body carries {project_id, actor}; reads take ?project_id=.
 */
import { arr, num, obj, str, usePolled, type Polled } from "./factory-floor";

const optNum = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const optStr = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const qs = (projectId: string) => `project_id=${encodeURIComponent(projectId)}`;
const enc = encodeURIComponent;

// ── Connect an app ──────────────────────────────────────────────────────────────────────────

export type ConnectPath = "boss" | "otel" | "collector" | "remote_write" | "metrics_url" | "url" | "alerts";
export type AlertTool = "alertmanager" | "grafana" | "sentry" | "datadog" | "cloudwatch_sns" | "pagerduty_v2" | "heartbeat" | "generic";
export const CONNECT_PATHS: { key: ConnectPath; label: string; tip: string }[] = [
  { key: "boss", label: "BOSS builds it", tip: "An app BOSS builds and deploys: it is watched already" },
  { key: "otel", label: "OpenTelemetry", tip: "The app's OTel SDK sends OTLP straight to BOSS" },
  { key: "collector", label: "A collector", tip: "An OpenTelemetry Collector forwards to BOSS" },
  { key: "remote_write", label: "Prometheus (remote write)", tip: "Your Prometheus remote-writes to BOSS" },
  { key: "metrics_url", label: "It has /metrics", tip: "BOSS scrapes its Prometheus endpoint" },
  { key: "url", label: "Just a URL", tip: "BOSS calls the URL on a schedule and checks the answer" },
  { key: "alerts", label: "It already alerts elsewhere", tip: "Another tool's alerts come into BOSS" },
];
export const ALERT_TOOLS: { key: AlertTool; label: string }[] = [
  { key: "alertmanager", label: "Alertmanager" }, { key: "grafana", label: "Grafana" }, { key: "sentry", label: "Sentry" },
  { key: "datadog", label: "Datadog" }, { key: "cloudwatch_sns", label: "CloudWatch (SNS)" },
  { key: "pagerduty_v2", label: "Anything that pages PagerDuty" }, { key: "heartbeat", label: "Heartbeat" },
];
/** Which inputs a path asks for (and nothing else). */
export function connectNeeds(path: ConnectPath, tool: AlertTool | null): { name: boolean; url: boolean; heartbeat: boolean } {
  return {
    name: path === "otel" || path === "metrics_url" || path === "url" || path === "collector" || path === "remote_write" || path === "alerts",
    url: path === "metrics_url" || path === "url",
    heartbeat: path === "alerts" && tool === "heartbeat",
  };
}
export const nameRequired = (path: ConnectPath) => path === "otel" || path === "metrics_url" || path === "url";

export type Connected = {
  path: string; word: string; serviceId: string | null; serviceName: string | null;
  source: { id: string; kind: string; tokenHint: string | null } | null;
  paste: { values: { name: string; value: string }[]; block: string | null } | null; shownOnce: boolean;
};
export function parseConnected(w: unknown): Connected {
  const o = obj(w);
  const svc = o.service ? obj(o.service) : null;
  const src = o.source ? obj(o.source) : null;
  const p = o.paste ? obj(o.paste) : null;
  return {
    path: str(o.path), word: str(o.word), serviceId: svc ? optStr(svc.id) : null, serviceName: svc ? optStr(svc.name) : null,
    source: src ? { id: str(src.id), kind: str(src.kind), tokenHint: optStr(src.token_hint) } : null,
    paste: p ? { values: arr(p.values).map((v) => { const x = obj(v); return { name: str(x.name), value: str(x.value) }; }),
                 block: optStr(p.block) } : null,
    shownOnce: o.shown_once === true,
  };
}
export type Live = { arrived: boolean; series: number; events: number; word: string; hint: string | null; rules: number };
export function parseLive(w: unknown): Live {
  const o = obj(w);
  return { arrived: o.arrived === true, series: num(o.series), events: num(o.events), word: str(o.word),
           hint: optStr(o.hint), rules: Array.isArray(o.rules) ? o.rules.length : num(o.rules) };
}
export const CONNECT_PATH = "/v1/monitoring/connect";
export const liveUrl = (projectId: string, serviceId: string, since: string) =>
  `/v1/monitoring/services/${enc(serviceId)}/live?${qs(projectId)}&since=${enc(since)}`;

// ── signal sources ──────────────────────────────────────────────────────────────────────────

export type Source = {
  id: string; name: string; kind: string; service: string | null; environment: string | null; tokenHint: string | null;
  lastSeenAt: string | null; lastRefusalWord: string | null; lastRefusalAt: string | null; enabled: boolean;
  accepted24h: number; refused24h: number; connected: boolean; oldTokenValidUntil: string | null; heartbeatEveryS: number | null;
};
export function parseSources(w: unknown): Source[] {
  return arr(obj(w).sources).map((x) => {
    const o = obj(x);
    return { id: str(o.id), name: str(o.name, str(o.kind)), kind: str(o.kind), service: optStr(o.service), environment: optStr(o.environment),
             tokenHint: optStr(o.token_hint), lastSeenAt: optStr(o.last_seen_at), lastRefusalWord: optStr(o.last_refusal_word),
             lastRefusalAt: optStr(o.last_refusal_at), enabled: o.enabled !== false, accepted24h: num(o.accepted_24h),
             refused24h: num(o.refused_24h), connected: o.connected === true, oldTokenValidUntil: optStr(o.old_token_valid_until),
             heartbeatEveryS: optNum(o.heartbeat_every_s) };
  });
}
export const sourcePath = (id: string, what: "rotate" | "test") => `/v1/signal-sources/${enc(id)}/${what}`;

// ── contact points ──────────────────────────────────────────────────────────────────────────

export type ContactKind = "console" | "email" | "slack_webhook" | "mattermost" | "webhook";
export const CONTACT_KINDS: { key: Exclude<ContactKind, "console">; label: string; field: "address" | "url"; placeholder: string }[] = [
  { key: "email", label: "Email", field: "address", placeholder: "oncall@example.com" },
  { key: "slack_webhook", label: "Slack", field: "url", placeholder: "https://hooks.slack.com/services/…" },
  { key: "mattermost", label: "Mattermost", field: "url", placeholder: "https://chat.example.com/hooks/…" },
  { key: "webhook", label: "Webhook", field: "url", placeholder: "https://example.com/boss-alerts" },
];
export const KIND_WORD: Record<string, string> = { console: "Console", email: "Email", slack_webhook: "Slack", mattermost: "Mattermost", webhook: "Webhook" };
export type ContactPoint = { id: string; name: string; kind: string; shows: string; verified: boolean; verifiedAt: string | null;
  hasSecret: boolean; ratePerMinute: number | null };
export const parseContactPoint = (x: unknown): ContactPoint => {
  const o = obj(x);
  return { id: str(o.id), name: str(o.name), kind: str(o.kind), shows: str(o.shows), verified: o.verified === true,
           verifiedAt: optStr(o.verified_at), hasSecret: o.has_secret === true, ratePerMinute: optNum(o.rate_per_minute) };
};
export const parseContactPoints = (w: unknown): ContactPoint[] => arr(obj(w).contact_points).map(parseContactPoint);
export const contactPath = (id?: string, what?: "test" | "verify") => (id ? `/v1/contact-points/${enc(id)}/${what}` : "/v1/contact-points");

// ── notification policies ───────────────────────────────────────────────────────────────────

export type Matcher = { name: string; op: string; value: string };
export type QuietHours = { start: string; end: string; timezone: string };
export type Policy = {
  id: string; parentId: string | null; position: number; name: string; matchers: Matcher[]; continueMatching: boolean;
  groupBy: string[]; groupWaitS: number | null; groupIntervalS: number | null; repeatIntervalS: number | null;
  contactPointIds: string[]; quietHours: QuietHours | null; ruleId: string | null;
};
const parseMatchers = (v: unknown): Matcher[] =>
  arr(v).map((m) => { const x = obj(m); return { name: str(x.name), op: str(x.op, "="), value: str(x.value) }; });
export function parsePolicies(w: unknown): Policy[] {
  return arr(obj(w).policies).map((x) => {
    const o = obj(x);
    const qh = o.quiet_hours ? obj(o.quiet_hours) : null;
    return { id: str(o.id), parentId: optStr(o.parent_id), position: num(o.position), name: str(o.name), matchers: parseMatchers(o.matchers),
             continueMatching: o.continue_matching === true, groupBy: arr(o.group_by).map(String), groupWaitS: optNum(o.group_wait_s),
             groupIntervalS: optNum(o.group_interval_s), repeatIntervalS: optNum(o.repeat_interval_s),
             contactPointIds: arr(o.contact_point_ids).map(String),
             quietHours: qh ? { start: str(qh.start), end: str(qh.end), timezone: str(qh.timezone, "UTC") } : null, ruleId: optStr(o.rule_id) };
  });
}
export const matchersWord = (ms: Matcher[]) => (ms.length ? ms.map((m) => `${m.name}${m.op}"${m.value}"`).join(", ") : "every alert");
/** The tree, depth-first in position order: [policy, depth]. */
export function policyTree(ps: Policy[]): [Policy, number][] {
  const kids = new Map<string | null, Policy[]>();
  for (const p of ps) kids.set(p.parentId, [...(kids.get(p.parentId) ?? []), p]);
  const out: [Policy, number][] = [];
  const seen = new Set<string>();
  const walk = (parent: string | null, depth: number) => {
    for (const p of (kids.get(parent) ?? []).sort((a, b) => a.position - b.position)) {
      if (seen.has(p.id)) continue;
      seen.add(p.id);
      out.push([p, depth]);
      walk(p.id, depth + 1);
    }
  };
  walk(null, 0);
  // An orphan (its parent gone) is still shown, at the top level.
  for (const p of ps) if (!seen.has(p.id)) { out.push([p, 0]); walk(p.id, 1); }
  return out;
}
export const policyPath = (id?: string) => (id ? `/v1/notification-policies/${enc(id)}` : "/v1/notification-policies");
export const PREVIEW_PATH = "/v1/notification-policies/preview";
export type RouteHit = { policyId: string; path: string[]; groupWaitS: number | null; repeatIntervalS: number | null;
  contactPoints: { id: string; name: string; kind: string; skipped: string | null }[]; quietUntil: string | null };
export type Preview = { urgency: string; routes: RouteHit[]; word: string };
export function parsePreview(w: unknown): Preview {
  const o = obj(w);
  return {
    urgency: str(o.urgency), word: str(o.word),
    routes: arr(o.routes).map((r) => {
      const x = obj(r);
      return { policyId: str(x.policy_id), path: arr(x.path).map(String), groupWaitS: optNum(x.group_wait_s),
               repeatIntervalS: optNum(x.repeat_interval_s), quietUntil: optStr(x.quiet_until),
               contactPoints: arr(x.contact_points).map((c) => { const y = obj(c); return { id: str(y.id), name: str(y.name), kind: str(y.kind), skipped: optStr(y.skipped) }; }) };
    }),
  };
}
/** `key=value` lines (or `key="value"`) into labels; blank and malformed lines are ignored. */
export function parseLabelLines(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split(/\n|,/)) {
    const m = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"?(.*?)"?\s*$/.exec(line);
    if (m && m[2]) out[m[1]] = m[2];
  }
  return out;
}

// ── notifications (the outbox) ──────────────────────────────────────────────────────────────

export type Notification = { id: string; kind: string; status: string; contactPoint: string | null; channel: string | null;
  subject: string; attempts: number; errorWord: string | null; sentAt: string | null; createdAt: string | null };
export const parseNotifications = (w: unknown): Notification[] => arr(obj(w).notifications).map((x) => {
  const o = obj(x);
  const cp = o.contact_point && typeof o.contact_point === "object" ? optStr(obj(o.contact_point).name) : optStr(o.contact_point);
  return { id: str(o.id), kind: str(o.kind), status: str(o.status), contactPoint: cp, channel: optStr(o.channel),
           subject: str(o.subject), attempts: num(o.attempts), errorWord: optStr(o.error_word), sentAt: optStr(o.sent_at),
           createdAt: optStr(o.created_at) };
});

// ── silences, maintenance windows, inhibit rules ────────────────────────────────────────────

export type Silence = { id: string; matchers: Matcher[]; startsAt: string | null; endsAt: string | null; reason: string; kind: string; createdBy: string | null };
export type MaintWindow = { id: string; name: string; matchers: Matcher[]; rrule: string; startsAt: string | null; durationMin: number;
  timezone: string; mode: string; enabled: boolean; activeUntil: string | null };
export type Inhibit = { id: string; name: string; source: Matcher[]; target: Matcher[]; equal: string[]; origin: string; enabled: boolean };
export type Silences = { silences: Silence[]; windows: MaintWindow[]; inhibit: Inhibit[] };
export function parseSilences(w: unknown): Silences {
  const o = obj(w);
  return {
    silences: arr(o.silences).map((x) => { const y = obj(x); return { id: str(y.id), matchers: parseMatchers(y.matchers), startsAt: optStr(y.starts_at),
      endsAt: optStr(y.ends_at), reason: str(y.reason), kind: str(y.kind), createdBy: optStr(y.created_by) }; }),
    windows: arr(o.windows).map((x) => { const y = obj(x); return { id: str(y.id), name: str(y.name), matchers: parseMatchers(y.matchers),
      rrule: str(y.rrule), startsAt: optStr(y.starts_at), durationMin: num(y.duration_min), timezone: str(y.timezone, "UTC"),
      mode: str(y.mode, "mute"), enabled: y.enabled !== false, activeUntil: optStr(y.active_until) }; }),
    inhibit: arr(o.inhibit_rules).map((x) => { const y = obj(x); return { id: str(y.id), name: str(y.name), source: parseMatchers(y.source_matchers),
      target: parseMatchers(y.target_matchers), equal: arr(y.equal).map(String), origin: str(y.origin), enabled: y.enabled !== false }; }),
  };
}
/** "Daily", "Weekly on Sat, Sun", "Once". */
export function rruleWord(rrule: string): string {
  if (!rrule) return "Once";
  if (/FREQ=DAILY/.test(rrule)) return "Daily";
  const days = /BYDAY=([A-Z,]+)/.exec(rrule)?.[1];
  return days ? `Weekly on ${days.split(",").map((d) => d[0] + d[1].toLowerCase()).join(", ")}` : "Weekly";
}

// ── hooks ───────────────────────────────────────────────────────────────────────────────────

const POLL_MS = 30_000;
const at = (path: string, projectId: string) => (projectId ? `${path}?${qs(projectId)}` : null);
export const useSignalSources = (projectId: string): Polled<Source[]> => usePolled(at("/v1/signal-sources", projectId), parseSources, () => POLL_MS);
export const useContactPoints = (projectId: string): Polled<ContactPoint[]> => usePolled(at("/v1/contact-points", projectId), parseContactPoints, () => POLL_MS);
export const usePolicies = (projectId: string): Polled<Policy[]> => usePolled(at("/v1/notification-policies", projectId), parsePolicies, () => POLL_MS);
export const useNotifications = (projectId: string): Polled<Notification[]> => usePolled(at("/v1/notifications", projectId), parseNotifications, () => POLL_MS);
export const useSilences = (projectId: string): Polled<Silences> => usePolled(at("/v1/silences", projectId), parseSilences, () => POLL_MS);

/** "30 s", "5 min", "4 h". */
export function secondsWord(s: number | null): string {
  if (s === null) return "—";
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${Math.round(s / 360) / 10} h`;
}
