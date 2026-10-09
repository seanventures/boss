/**
 * App › Security and the Factory's before/after report (APP_TEMPLATES_AND_SECURITY_LOOP.md §4.6):
 * the shapes `/v1/security/*` answers, read defensively, and the small rules the screens share.
 *
 * Reads and writes go same-origin through app/v1/[...path] with the person's session
 * (lib/identity/api.ts: CSRF on every write, refusals as IdentityError).
 */
import { IdentityError, idGet, idPost } from "./identity/api";
import { refusalFrom } from "../lib/start-refusal";

export const SEVERITIES = ["critical", "high", "medium", "low", "info"] as const;
export type Severity = (typeof SEVERITIES)[number];
export const TOOLS = ["semgrep", "osv", "zap"] as const;
export type Tool = (typeof TOOLS)[number];
export const TOOL_LABEL: Record<string, string> = { semgrep: "Code", osv: "Dependencies", zap: "Running app" };
export const TOOL_TIP: Record<string, string> = {
  semgrep: "Semgrep: the app's source code",
  osv: "OSV-Scanner: known advisories in its dependencies",
  zap: "OWASP ZAP: the running app, started in the scan's own sandbox",
};
export const STATUS_LABEL: Record<string, string> = {
  open: "Open", fixed: "Fixed", accepted: "Accepted", false_positive: "False positive",
};

type O = Record<string, unknown>;
const obj = (v: unknown): O => (v && typeof v === "object" && !Array.isArray(v) ? (v as O) : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : String(v));
const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);
const sev = (v: unknown): Severity => (SEVERITIES as readonly string[]).includes(str(v)) ? (str(v) as Severity) : "info";

export type ScanTool = { name: string; status: string; version: string; rulesDigest: string; durationMs: number; findings: number; error: string };
export type Scan = {
  id: string; appKey: string; status: string; commit: string; startedAt: string; endedAt: string;
  tools: ScanTool[]; total: number; bySeverity: Record<Severity, number>;
  byTool: Record<string, Record<Severity, number>>; failedTools: string[]; error: string;
  workflowRunId: string; zapMode: string;
};
export type Finding = {
  id: string; fingerprint: string; tool: string; ruleId: string; title: string; severity: Severity;
  confidence: string; cwe: string; owasp: string; location: O; evidence: O; status: string;
  triagedBy: string; triageReason: string; firstSeenScanId: string;
};

function sevCounts(v: unknown): Record<Severity, number> {
  const o = obj(v);
  return Object.fromEntries(SEVERITIES.map((s) => [s, num(o[s])])) as Record<Severity, number>;
}

export function parseScan(v: unknown): Scan {
  const o = obj(v);
  const counts = obj(o.counts);
  const target = obj(o.target);
  const byTool = obj(counts.by_tool);
  return {
    id: str(o.id), appKey: str(o.app_key), status: str(o.status), commit: str(o.commit_sha),
    startedAt: str(o.started_at), endedAt: str(o.ended_at), workflowRunId: str(o.workflow_run_id),
    tools: arr(o.tools).map((t) => {
      const x = obj(t);
      return { name: str(x.name), status: str(x.status), version: str(x.version), rulesDigest: str(x.rules_digest),
               durationMs: num(x.duration_ms), findings: num(x.findings), error: str(x.error) };
    }),
    total: num(counts.total), bySeverity: sevCounts(counts.by_severity),
    byTool: Object.fromEntries(Object.entries(byTool).map(([k, c]) => [k, sevCounts(c)])),
    failedTools: arr(counts.failed_tools).map(str), error: str(counts.error),
    zapMode: str(obj(target.zap).mode),
  };
}

export function parseFinding(v: unknown): Finding {
  const o = obj(v);
  return {
    id: str(o.id), fingerprint: str(o.fingerprint), tool: str(o.tool), ruleId: str(o.rule_id), title: str(o.title),
    severity: sev(o.severity), confidence: str(o.confidence), cwe: str(o.cwe), owasp: str(o.owasp),
    location: obj(o.location), evidence: obj(o.evidence), status: str(o.status) || "open",
    triagedBy: str(o.triaged_by), triageReason: str(o.triage_reason), firstSeenScanId: str(o.first_seen_scan_id),
  };
}

/** Where a finding is, in a line: file:line (function), package@version, or METHOD /path [param]. */
export function whereOf(f: Pick<Finding, "tool" | "location">): string {
  const l = f.location;
  if (f.tool === "semgrep") return `${str(l.file)}${l.line ? `:${str(l.line)}` : ""}${l.function ? ` (${str(l.function)})` : ""}`;
  if (f.tool === "osv") return `${str(l.package)}@${str(l.version)}${l.lockfile ? ` · ${str(l.lockfile)}` : ""}`;
  return `${str(l.method)} ${str(l.path)}${l.param ? ` [${str(l.param)}]` : ""}`;
}

export type FindingFilter = { tool: string; severity: string; status: string };
export const NO_FILTER: FindingFilter = { tool: "", severity: "", status: "" };

export function filterFindings(all: Finding[], f: FindingFilter): Finding[] {
  return all.filter((x) => (!f.tool || x.tool === f.tool) && (!f.severity || x.severity === f.severity)
    && (!f.status || x.status === f.status));
}

// ---- a review run (the security_review workflow) ------------------------------------------------

export type Recommendation = {
  id: string; title: string; fix: string; priority: string; effort: string; fingerprints: string[];
  riskIfUnfixed: string; falsePositiveReason: string; source: "panel" | "derived"; selected: boolean;
  decidedBy: string; decidedAt: string;
};
export type ReviewStep = { id: string; status: string; note: string; error: string; scanId: string; passed: boolean | null; codeTaskRunId: string; afterScanId: string };
export type ReviewFinding = { id: string; title: string; severity: Severity; tool: string; status: string; where: string };
export type Review = {
  runId: string; status: string; appKey: string; createdAt: string; steps: ReviewStep[];
  recommendations: Recommendation[]; awaitingChoice: boolean; decisionId: string; reportArtifactId: string;
  scanId: string; findings: Record<string, ReviewFinding>;
};

export function parseRecommendation(v: unknown): Recommendation {
  const o = obj(v);
  return {
    id: str(o.id), title: str(o.title), fix: str(o.fix), priority: str(o.priority) || "P3", effort: str(o.effort),
    fingerprints: arr(o.fingerprints).map(str), riskIfUnfixed: str(o.risk_if_unfixed),
    falsePositiveReason: str(o.false_positive_reason), source: o.source === "derived" ? "derived" : "panel",
    selected: o.selected === true, decidedBy: str(o.decided_by), decidedAt: str(o.decided_at),
  };
}

export function parseReview(v: unknown): Review {
  const o = obj(v);
  return {
    runId: str(o.run_id), status: str(o.status), appKey: str(o.app_key), createdAt: str(o.created_at),
    steps: arr(o.steps).map((s) => {
      const x = obj(s);
      return { id: str(x.id), status: str(x.status), note: str(x.note), error: str(x.error), scanId: str(x.scan_id),
               passed: typeof x.passed === "boolean" ? x.passed : null, codeTaskRunId: str(x.code_task_run_id),
               afterScanId: str(x.after_scan_id) };
    }),
    recommendations: arr(o.recommendations).map(parseRecommendation),
    awaitingChoice: o.awaiting_choice === true, decisionId: str(o.decision_id),
    reportArtifactId: str(o.report_artifact_id), scanId: str(o.scan_id),
    findings: Object.fromEntries(Object.entries(obj(o.findings)).map(([fp, f]) => {
      const x = obj(f);
      return [fp, { id: str(x.id), title: str(x.title), severity: sev(x.severity), tool: str(x.tool),
                    status: str(x.status), where: str(x.where) }];
    })),
  };
}

/** The steps, as the review card names them. */
export const STEP_LABEL: Record<string, string> = {
  scan: "Scan", panel: "Panel", decide: "Choose", fix: "Fix", rescan: "Re-scan", rereview: "Re-review",
  fix_2: "Fix 2", rescan_2: "Re-scan 2", rereview_2: "Re-review 2", report: "Report",
};
export const STEP_TIP: Record<string, string> = {
  scan: "Semgrep, OSV-Scanner and ZAP on the app",
  panel: "A panel chaired by the security lens recommends fixes",
  decide: "A person picks which fixes to build",
  fix: "The app's next build makes the chosen fixes",
  rescan: "The same tools and rules, on the fix",
  rereview: "Passes only if every targeted finding is gone and nothing new is high",
  fix_2: "Round 2: fixes what the re-scan still found",
  rescan_2: "Round 2 re-scan",
  rereview_2: "Round 2 re-review: the last",
  report: "The before/after, in the Factory",
};

/** A step's state in one word. `not_applicable` rows (round 2 after a pass) read "Not needed". */
export function stepWord(s: ReviewStep): string {
  if (s.status === "succeeded" && /not needed|nothing (left )?to|no fix was built|nobody chose|found nothing/i.test(s.note)) return "Not needed";
  if (s.status === "succeeded" && s.passed === false) return "Changes asked";
  if (s.status === "succeeded") return "Done";
  if (s.status === "waiting" && s.id === "decide") return "Needs you";
  if (s.status === "waiting" || s.status === "leased" || s.status === "running" || s.status === "ready") return "Working";
  if (s.status === "failed" || s.status === "deadlettered") return "Failed";
  if (s.status === "skipped") return "Skipped";
  return "Waiting";
}

export function reviewWord(r: Review): string {
  if (r.awaitingChoice) return "Needs your choice";
  if (r.status === "completed") {
    const last = [...r.steps].reverse().find((s) => s.id.startsWith("rereview") && s.passed !== null);
    return last ? (last.passed ? "Passed" : "Did not pass") : "Done";
  }
  if (r.status === "failed" || r.status === "blocked" || r.status === "deadlettered") return "Stopped";
  if (r.status === "cancelled") return "Cancelled";
  return "Running";
}

/** The two scans a report compares: the first, and the last re-scan that ran. */
export function reportScans(r: Review): { before: string; after: string } | null {
  const before = r.steps.find((s) => s.id === "scan")?.scanId ?? "";
  const after = ["rescan_2", "rescan"].map((id) => r.steps.find((s) => s.id === id)?.scanId ?? "").find(Boolean) ?? "";
  return before && after ? { before, after } : null;
}

// ---- the report ----------------------------------------------------------------------------------

export type ReportItem = { id: string; fingerprint: string; tool: string; ruleId: string; title: string; severity: Severity; where: string; triageReason: string };
export type Report = {
  appKey: string; before: Scan; after: Scan; rulesDiffer: boolean;
  differences: { tool: string; field: string; before: string; after: string }[];
  severityBefore: Record<Severity, number>; severityAfter: Record<Severity, number>;
  fixed: ReportItem[]; stillOpen: ReportItem[]; moved: ReportItem[]; removed: ReportItem[]; notRescanned: ReportItem[]; new: ReportItem[];
  summary: { fixed: number; stillOpen: number; moved: number; removed: number; notRescanned: number; new: number; newHighOrCritical: number };
};

function item(v: unknown): ReportItem {
  const o = obj(v);
  return { id: str(o.id), fingerprint: str(o.fingerprint), tool: str(o.tool), ruleId: str(o.rule_id), title: str(o.title),
           severity: sev(o.severity), where: str(o.where), triageReason: str(o.triage_reason) };
}

export function parseReport(v: unknown): Report {
  const o = obj(v);
  const s = obj(o.summary);
  return {
    appKey: str(o.app_key), before: parseScan(o.before), after: parseScan(o.after), rulesDiffer: o.rules_differ === true,
    differences: arr(o.differences).map((d) => { const x = obj(d); return { tool: str(x.tool), field: str(x.field), before: str(x.before), after: str(x.after) }; }),
    severityBefore: sevCounts(o.severity_before), severityAfter: sevCounts(o.severity_after),
    fixed: arr(o.fixed).map(item), stillOpen: arr(o.still_open).map(item), moved: arr(o.moved).map(item),
    removed: arr(o.removed).map(item), notRescanned: arr(o.not_rescanned).map(item), new: arr(o.new).map(item),
    summary: { fixed: num(s.fixed), stillOpen: num(s.still_open), moved: num(s.moved), removed: num(s.removed),
               notRescanned: num(s.not_rescanned), new: num(s.new), newHighOrCritical: num(s.new_high_or_critical) },
  };
}

export type TargetOutcome = "Fixed" | "Removed" | "Justified" | "Still open" | "Moved" | "Not re-scanned";
export type TargetRow = { fingerprint: string; title: string; severity: Severity; tool: string; where: string; outcome: TargetOutcome };

/** Each targeted finding (the chosen recommendations') with what the re-scan says of it — the same
 *  rule the re-review applies: gone = Fixed or Removed; a person's triage = Justified. */
export function targetRows(rep: Report, targeted: string[]): TargetRow[] {
  const index = new Map<string, { it: ReportItem; o: TargetOutcome }>();
  for (const [list, o] of [[rep.fixed, "Fixed"], [rep.removed, "Removed"], [rep.stillOpen, "Still open"],
                           [rep.moved, "Moved"], [rep.notRescanned, "Not re-scanned"]] as [ReportItem[], TargetOutcome][]) {
    for (const it of list) if (!index.has(it.fingerprint)) index.set(it.fingerprint, { it, o });
  }
  return targeted.map((fp) => {
    const hit = index.get(fp);
    if (!hit) return { fingerprint: fp, title: fp.slice(0, 12), severity: "info" as Severity, tool: "", where: "", outcome: "Not re-scanned" as TargetOutcome };
    const justified = hit.o === "Still open" && !!hit.it.triageReason;
    return { fingerprint: fp, title: hit.it.title, severity: hit.it.severity, tool: hit.it.tool, where: hit.it.where,
             outcome: justified ? "Justified" : hit.o };
  });
}

/** The plain-language line at the top (spec §4.6). */
export function reportHeadline(rep: Report, targets?: TargetRow[]): string {
  const total = SEVERITIES.reduce((n, s) => n + rep.severityBefore[s], 0);
  const parts = [`${total} found before`, `${rep.summary.fixed} fixed`, `${rep.summary.stillOpen} still open`,
                 `${rep.summary.new} new${rep.summary.newHighOrCritical ? ` (${rep.summary.newHighOrCritical} high)` : ""}`];
  if (targets && targets.length) {
    const gone = targets.filter((t) => t.outcome === "Fixed" || t.outcome === "Removed").length;
    parts.unshift(`${gone} of ${targets.length} targeted fixed`);
  }
  return parts.join(" · ");
}

export function shortSha(scan: Scan): string {
  return scan.commit ? scan.commit.slice(0, 7) : scan.id.slice(0, 12);
}

// ---- calls ---------------------------------------------------------------------------------------

const q = (o: Record<string, string | undefined>) =>
  Object.entries(o).filter(([, v]) => v).map(([k, v]) => `${k}=${encodeURIComponent(v!)}`).join("&");

export const securityApi = {
  scans: async (projectId: string, appKey: string) =>
    arr(obj(await idGet(`/v1/security/scans?${q({ project_id: projectId, app_key: appKey, limit: "20" })}`)).scans).map(parseScan),
  findings: async (scanId: string) =>
    arr(obj(await idGet(`/v1/security/scans/${encodeURIComponent(scanId)}/findings`)).findings).map(parseFinding),
  reviews: async (projectId: string, appKey: string) =>
    arr(obj(await idGet(`/v1/security/reviews?${q({ project_id: projectId, app_key: appKey })}`)).reviews).map(parseReview),
  review: async (runId: string) => parseReview(await idGet(`/v1/security/reviews/${encodeURIComponent(runId)}`)),
  report: async (projectId: string, appKey: string, before: string, after: string) =>
    parseReport(await idGet(`/v1/security/reports/${encodeURIComponent(appKey)}?${q({ project_id: projectId, before, after })}`)),
  reportMarkdownHref: (projectId: string, appKey: string, before: string, after: string) =>
    `/v1/security/reports/${encodeURIComponent(appKey)}?${q({ project_id: projectId, before, after, format: "md" })}`,
  scanNow: (projectId: string, appKey: string) =>
    idPost<{ scan_id: string }>("/v1/security/scans", { project_id: projectId, app_key: appKey }),
  /** The full loop: POST /v1/delivery-workflows/start with workflow_key=security_review. A refusal
   *  (409 with the readiness checklist, say) is flattened to its sentence (lib/start-refusal). */
  startReview: async (projectId: string, appKey: string, focus: string) => {
    try {
      return await idPost<{ run_id?: string; id?: string }>("/v1/delivery-workflows/start", {
        project_id: projectId, workflow_key: "security_review",
        input: { app_key: appKey, ...(focus.trim() ? { directive: focus.trim() } : {}) },
      });
    } catch (e) {
      if (e instanceof IdentityError && e.status !== 401 && e.status !== 403) throw new Error(refusalFrom(e.status, e.body).error);
      throw e;
    }
  },
  triage: (findingId: string, status: "accepted" | "false_positive", reason: string) =>
    idPost(`/v1/security/findings/${encodeURIComponent(findingId)}/triage`, { status, reason }),
  choose: (runId: string, selected: string[], accept: { finding_id: string; reason: string }[] = []) =>
    idPost(`/v1/security/reviews/${encodeURIComponent(runId)}/selection`, { selected, accept }),
  /** "Fix selected": record the ticks AND approve the review's gate as ONE server action. The
   * server checks the right to approve first; a refusal records nothing. */
  chooseAndApprove: (runId: string, selected: string[], accept: { finding_id: string; reason: string }[], note: string) =>
    idPost(`/v1/security/reviews/${encodeURIComponent(runId)}/selection`, { selected, accept, approve: true, note }),
  approve: (decisionId: string, note: string) =>
    idPost(`/v1/decisions/${encodeURIComponent(decisionId)}/approve`, { note }),
};
