import type { RunAction } from "../app/delivery-workflows-ui";
/**
 * The Command Center's data: what is running, what needs the operator, how recent runs went.
 *
 * Read from GET /v1/console/command-center (boss_engine.services.command_center), which reads
 * WORKFLOW RUNS. The page it replaces read `tasks`: a run started by Build creates no task, so
 * the landing page never showed the thing the operator had just started, and "Live
 * coordination" animated idle orbs from a field nothing populates.
 *
 * Pure helpers here (hrefs, labels, durations) so the rules are testable without a browser.
 */
import { API_URL, authHeaders } from "./client";

export type AgentOnStep = {
  role_key: string | null;
  name: string | null;
  how: "picked" | "named" | "default" | "person" | "auto" | "none";
};

export type RunningRun = {
  run_id: string;
  workflow_key?: string | null;
  workflow_name: string;
  subject: string;
  status: string;
  steps_done: number;
  step_count: number;
  started_at: string | null;
  step_id: string | null;
  step_label: string | null;
  step_status: string | null;
  agent: AgentOnStep | null;
  since: string | null;
  elapsed_seconds: number | null;
  blocked_reason: string | null;
};

export type NeedsYouItem = {
  kind: "approval" | "failed_step" | "readiness";
  decision_kind?: string;
  title: string;
  detail: string;
  what_to_do?: string | null;
  fix_area?: string | null;
  /** Where in that area: Repo and models lands on the row (`focus=repository|models`). */
  fix_focus?: string | null;
  fix_label?: string | null;
  decision_id?: string | null;
  run_id?: string | null;
  step_id?: string | null;
  workflow_name?: string | null;
  actions: string[];
  /** On a failed step: the run's own answers (workflow_run_actions.stopped_for_operator), the
   *  same ones the Workflow screen's run card offers. Only these are offered here. */
  answers?: RunAction[];
  at: string | null;
};

export type RecentOutcome = {
  run_id: string;
  workflow_name: string;
  subject: string;
  outcome: string;
  finished_at: string | null;
  error: string | null;
};

export type CommandCenterData = {
  apiReachable: boolean;
  blockedReason?: string;
  observed_at?: string;
  running: RunningRun[];
  needs_you: NeedsYouItem[];
  recent: RecentOutcome[];
  counts: {
    running: number;
    needs_you: number;
    approvals: number;
    /** Expired hold-default decisions: nothing was applied and they can still be answered.
     *  Counted apart from approvals, by the rule the Approvals inbox groups them with. */
    expired_answerable: number;
    failed_steps: number;
    readiness: number;
    succeeded_7d: number;
    failed_7d: number;
  };
};

export const EMPTY_COUNTS: CommandCenterData["counts"] = {
  running: 0, needs_you: 0, approvals: 0, expired_answerable: 0, failed_steps: 0, readiness: 0,
  succeeded_7d: 0, failed_7d: 0,
};

export function emptyCommandCenter(reason?: string): CommandCenterData {
  return { apiReachable: false, blockedReason: reason, running: [], needs_you: [], recent: [],
           counts: { ...EMPTY_COUNTS } };
}

/** Normalise an API body into the shape the screen renders, tolerating missing fields. */
export function commandCenterFrom(body: unknown): CommandCenterData {
  const b = (body ?? {}) as Partial<CommandCenterData>;
  return {
    apiReachable: true,
    observed_at: b.observed_at,
    running: Array.isArray(b.running) ? b.running : [],
    needs_you: Array.isArray(b.needs_you) ? b.needs_you : [],
    recent: Array.isArray(b.recent) ? b.recent : [],
    counts: { ...EMPTY_COUNTS, ...(b.counts ?? {}) },
  };
}

/** Server-side first paint. The screen then polls through the console's /v1 proxy. */
export async function fetchCommandCenter(projectId: string, enabled = true):
    Promise<CommandCenterData> {
  if (!enabled) return emptyCommandCenter();
  try {
    const r = await fetch(
      `${API_URL}/v1/console/command-center?project_id=${encodeURIComponent(projectId)}`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) {
      let said = "";
      try { said = String(((await r.json()) as { detail?: unknown }).detail ?? ""); } catch {}
      return emptyCommandCenter(said || `The API answered ${r.status}.`);
    }
    return commandCenterFrom(await r.json());
  } catch (error) {
    return emptyCommandCenter(error instanceof Error ? error.message : "API unreachable");
  }
}

function withOrg(params: URLSearchParams, orgId?: string | null): string {
  const out = new URLSearchParams();
  if (orgId) out.set("org", orgId);
  params.forEach((v, k) => out.set(k, v));
  return `/?${out.toString()}`;
}

/** The run, opened in Workflow. */
export function runHref(runId: string, orgId?: string | null): string {
  return withOrg(new URLSearchParams({ area: "delivery_workflows", wfr: runId }), orgId);
}

/** The decision, highlighted in Approvals. */
export function decisionHref(decisionId: string, orgId?: string | null): string {
  return withOrg(new URLSearchParams({ area: "decisions", decision: decisionId,
                                       decision_filter: "needs_action" }), orgId);
}

/** The Approvals inbox, opened on one of its filters. */
export function decisionsHref(filter: "needs_action" | "expired", orgId?: string | null): string {
  return withOrg(new URLSearchParams({ area: "decisions", decision_filter: filter }), orgId);
}

export function areaHref(area: string, orgId?: string | null, focus?: string | null): string {
  return withOrg(new URLSearchParams({ area, ...(focus ? { focus } : {}) }), orgId);
}

/** "12m", "1h 05m", "3d 4h" — how long something has been where it is. */
export function elapsedLabel(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "";
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${String(m % 60).padStart(2, "0")}m`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

/** Who is on the step, in a phrase. */
export function agentPhrase(agent: AgentOnStep | null | undefined): string {
  if (!agent) return "";
  switch (agent.how) {
    case "picked": return `${agent.name} — picked by BOSS`;
    case "named": return `${agent.name}`;
    case "default": return `${agent.name}`;
    case "person": return "Waiting on you";
    case "auto": return "BOSS picks the agent when the step starts";
    default: return "";
  }
}

/** The pill on a needs-you row. */
export function needsYouLabel(item: NeedsYouItem): string {
  if (item.kind === "failed_step") return "Failed step";
  if (item.kind === "readiness") return "Setup";
  switch (item.decision_kind) {
    case "deploy_gate": return "Deploy gate";
    case "pull_request": return "PR merge";
    case "workflow_gate": return "Approval";
    case "refusal": return "Refused";
    default: return "Approval";
  }
}

export function outcomeTone(outcome: string): "green" | "red" | "muted" {
  if (outcome === "succeeded") return "green";
  if (outcome === "failed" || outcome === "deadlettered") return "red";
  return "muted";
}
