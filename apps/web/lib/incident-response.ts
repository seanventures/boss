/**
 * An app's incident, as the Observability app drawer's Incident card reads it
 * (GET /v1/reliability/incidents/{id}/response; engine services/incident_response.response_view).
 *
 * Pure: the wire parse, the status chip's word (derived here from the data, never sent), the
 * response's step strip, and the hrefs the card links to. The card is
 * app/observability/incident-card.tsx.
 */
import { arr, num, obj, str } from "./factory-floor";
import type { Tone } from "./infra-view";

const optStr = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const optNum = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

export type IncidentEvidence = { kind: string; ref: string; at: string | null; payload: Record<string, unknown> };
export type IncidentReport = { reporterTargetId: string; at: string | null; id: string; title: string; refs: string[]; count: number };
export type IncidentRoute = { route: string; requests: number; errors: number; errorRate: number | null };
export type IncidentStep = { stepId: string; status: string };

export type IncidentResponse = {
  incident: { id: string; title: string; service: string; severity: string; status: string;
              confidence: number | null; firstSeen: string | null; lastSeen: string | null };
  app: { targetId: string; appName: string; environment: string } | null;
  deploy: { commitSha: string | null; previousCommitSha: string | null; deployedAt: string | null } | null;
  alert: { firedAt: string | null; errorRatePercent: number | null; windowMinutes: number | null; minRequests: number | null;
           errorRate: number | null; requests: number | null; errors: number | null; routes: IncidentRoute[] } | null;
  verification: { state: string; summary: string; at: string | null; independent: string | null } | null;
  reportedOnly: boolean;
  evidence: IncidentEvidence[];
  reports: IncidentReport[];
  response: { id: string; status: string; startedAt: string | null; steps: IncidentStep[] } | null;
  autoRespond: { subscriptionId: string; enabled: boolean; onBehalfOf: string | null } | null;
  reportReady: boolean;
  respondInput: Record<string, unknown>;
};

export function parseIncidentResponse(w: unknown): IncidentResponse {
  const o = obj(w);
  const i = obj(o.incident);
  const app = o.app ? obj(o.app) : null;
  const d = o.deploy ? obj(o.deploy) : null;
  const a = o.alert ? obj(o.alert) : null;
  const rule = a ? obj(a.rule) : {};
  const v = o.verification ? obj(o.verification) : null;
  const r = o.response ? obj(o.response) : null;
  const auto = o.auto_respond ? obj(o.auto_respond) : null;
  return {
    incident: { id: str(i.id), title: str(i.title), service: str(i.service), severity: str(i.severity), status: str(i.status),
                confidence: optNum(i.confidence), firstSeen: optStr(i.first_seen), lastSeen: optStr(i.last_seen) },
    app: app ? { targetId: str(app.target_id), appName: str(app.app_name), environment: str(app.environment) } : null,
    deploy: d ? { commitSha: optStr(d.commit_sha), previousCommitSha: optStr(d.previous_commit_sha), deployedAt: optStr(d.deployed_at) } : null,
    alert: a ? {
      firedAt: optStr(a.fired_at), errorRatePercent: optNum(rule.error_rate_percent), windowMinutes: optNum(rule.window_minutes),
      minRequests: optNum(rule.min_requests), errorRate: optNum(a.error_rate), requests: optNum(a.requests), errors: optNum(a.errors),
      routes: arr(a.routes).map((x) => { const y = obj(x); return { route: str(y.route), requests: num(y.requests), errors: num(y.errors), errorRate: optNum(y.error_rate) }; }),
    } : null,
    verification: v ? { state: str(v.state), summary: str(v.summary), at: optStr(v.at), independent: optStr(v.independent) } : null,
    reportedOnly: o.reported_only === true,
    evidence: arr(o.evidence).map((x) => { const y = obj(x); return { kind: str(y.kind), ref: str(y.ref), at: optStr(y.at), payload: obj(y.payload) }; }),
    reports: arr(o.reports).map((x) => {
      const y = obj(x);
      return { reporterTargetId: str(y.reporter_target_id), at: optStr(y.at), id: str(y.id), title: str(y.title),
               refs: arr(y.refs).filter((s): s is string => typeof s === "string" && !!s), count: num(y.count, 1) || 1 };
    }),
    response: r ? { id: str(r.id), status: str(r.status), startedAt: optStr(r.started_at),
                    steps: arr(r.steps).map((x) => { const y = obj(x); return { stepId: str(y.step_id), status: str(y.status) }; }) } : null,
    autoRespond: auto ? { subscriptionId: str(auto.subscription_id), enabled: auto.enabled === true, onBehalfOf: optStr(auto.on_behalf_of) } : null,
    reportReady: o.report_ready === true,
    respondInput: obj(o.respond_input),
  };
}

// ── the status chip (derived, never sent) ─────────────────────────────────────────────────────

export type IncidentStatus = "reported" | "alerting" | "responding" | "verifying" | "resolved";
/** The run states engine incident_response counts as a response in flight (_ACTIVE_RUN_STATES). */
const ACTIVE_RUN = new Set(["queued", "validating", "running", "waiting", "paused", "blocked"]);
/** The step states engine workflows counts as active (ACTIVE_STEP_STATES). */
const ACTIVE_STEP = new Set(["ready", "leased", "running", "waiting"]);

export const STATUS_WORDS: Record<IncidentStatus, { word: string; tone: Tone; tip: string }> = {
  reported: { word: "Reported", tone: "amber", tip: "An app reported this; BOSS's monitor has not seen it fail" },
  alerting: { word: "Alerting", tone: "red", tip: "The error rate is over its limit and nothing is responding" },
  responding: { word: "Responding", tone: "accent", tip: "Incident Response is running: investigate, fix, ship" },
  verifying: { word: "Verifying", tone: "accent", tip: "The fix shipped; BOSS's monitor is checking the app recovered" },
  resolved: { word: "Resolved", tone: "green", tip: "BOSS's monitor saw the app recover" },
};

export const responseRunning = (r: IncidentResponse): boolean => !!r.response && ACTIVE_RUN.has(r.response.status);

export function incidentStatus(r: IncidentResponse): IncidentStatus {
  const s = r.incident.status;
  if (s === "resolved" || s === "dismissed") return "resolved";
  if (responseRunning(r)) {
    const verify = r.response!.steps.find((x) => x.stepId === "verify");
    return verify && ACTIVE_STEP.has(verify.status) ? "verifying" : "responding";
  }
  return r.reportedOnly ? "reported" : "alerting";
}

// ── the response's step strip ─────────────────────────────────────────────────────────────────

/** The catalogue's incident_response steps, in order, with the card's short words. */
export const RESPONSE_STEPS: { id: string; label: string }[] = [
  { id: "investigate", label: "Investigate" }, { id: "build", label: "Build" }, { id: "ci", label: "CI" },
  { id: "report", label: "Run report" }, { id: "deploy", label: "Deploy" }, { id: "verify", label: "Verify" },
  { id: "incident_report", label: "Write-up" },
];

export function stepTone(status: string | null): Tone {
  if (!status) return "hollow";
  if (status === "succeeded") return "green";
  if (status === "skipped") return "grey";
  if (status === "failed" || status === "deadlettered" || status === "cancelled") return "red";
  if (status === "waiting") return "amber";
  if (ACTIVE_STEP.has(status)) return "accent";
  return "hollow";
}

/** The strip: the catalogue's steps in order (any extra step the run has follows them). */
export function responseStrip(steps: IncidentStep[]): { id: string; label: string; status: string | null }[] {
  const by = new Map(steps.map((s) => [s.stepId, s.status]));
  const known = RESPONSE_STEPS.map((s) => ({ ...s, status: by.get(s.id) ?? null }));
  const extra = steps.filter((s) => !RESPONSE_STEPS.some((k) => k.id === s.stepId))
    .map((s) => ({ id: s.stepId, label: s.stepId.replace(/_/g, " "), status: s.status }));
  return [...known, ...extra];
}

// ── what the card reads out of the evidence ──────────────────────────────────────────────────

export const evidenceOf = (r: IncidentResponse, kind: string): IncidentEvidence | null =>
  [...r.evidence].reverse().find((e) => e.kind === kind) ?? null;

/** The fix's pull request, when any evidence carries one (it is optional in the payload). */
export function pullRequestUrl(r: IncidentResponse): string | null {
  for (const e of [...r.evidence].reverse()) {
    const u = e.payload.pull_request_url;
    if (typeof u === "string" && /^https?:\/\//.test(u)) return u;
  }
  return null;
}

/** The reporting apps, by name where the customer_reports evidence names them. */
export function reportGroups(r: IncidentResponse): { reporter: string; reports: number; refs: string[] }[] {
  const names = new Map<string, string>();
  for (const e of r.evidence) {
    if (e.kind !== "customer_reports") continue;
    const rep = obj(e.payload.reporter);
    const id = str(rep.target_id) || e.ref;
    if (id && typeof rep.app_name === "string" && rep.app_name) names.set(id, rep.app_name);
  }
  const groups = new Map<string, { reporter: string; reports: number; refs: string[] }>();
  for (const rep of r.reports) {
    const g = groups.get(rep.reporterTargetId) ?? { reporter: names.get(rep.reporterTargetId) ?? rep.reporterTargetId, reports: 0, refs: [] };
    g.reports += rep.count;
    for (const ref of rep.refs) if (!g.refs.includes(ref)) g.refs.push(ref);
    groups.set(rep.reporterTargetId, g);
  }
  return [...groups.values()];
}

export const sha7 = (s: string | null): string | null => (s ? s.slice(0, 7) : null);
/** "release_changed_failing_frame" → "release changed failing frame". */
export const basisWords = (b: unknown): string => str(b).replace(/_/g, " ");

// ── urls ─────────────────────────────────────────────────────────────────────────────────────

const enc = encodeURIComponent;
export const incidentResponseUrl = (projectId: string, incidentId: string) =>
  `/v1/reliability/incidents/${enc(incidentId)}/response?project_id=${enc(projectId)}`;
export const incidentReportUrl = (projectId: string, incidentId: string, format: "json" | "md") =>
  `/v1/reliability/incidents/${enc(incidentId)}/report?project_id=${enc(projectId)}&format=${format}`;
/** The run, opened in Workflows (the console's run page). */
export const incidentRunHref = (runId: string) => `?area=delivery_workflows&wfr=${enc(runId)}`;

/** The event subscription "Respond automatically" makes (POST /v1/event-subscriptions). */
export const AUTO_RESPOND = { event_type: "incident.opened", sink_kind: "workflow", sink_ref: "incident_response" } as const;
export const RESPONSE_POLL_MS = 30_000;
export const RESPONSE_POLL_RUNNING_MS = 10_000;
