/**
 * AGENT FEEDBACK (BOSSAPP-889; API routers/agent_feedback.py): what agents complained about or
 * suggested with `report_friction`, grouped by what it is about. Parsing is pure and defensive;
 * the one write (a person acknowledges, resolves or dismisses a record) goes through obsWrite.
 */
import { aaRead, type ReadResult } from "./ambient";
import { obsWrite, type WriteResult } from "../app/observability/obs-write";

export type FeedbackStatus = "open" | "acknowledged" | "resolved" | "dismissed";
export const STATUSES: FeedbackStatus[] = ["open", "acknowledged", "resolved", "dismissed"];
export type Feedback = {
  id: string; role_key: string | null; run_ref: string | null; kind: "complaint" | "suggestion";
  target_kind: string; target: string; body: string; evidence: string[]; status: FeedbackStatus;
  resolution: string | null; resolved_by: string | null; created_at: string | null;
};
export type FeedbackGroup = {
  target_kind: string; target: string; complaints: number; suggestions: number; open: number;
  roles: string[]; latest_at: string | null; items: Feedback[];
};

const obj = (v: unknown): Record<string, unknown> =>
  (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown) => (typeof v === "string" ? v : "");
const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : 0);

export function parseFeedback(w: unknown): Feedback {
  const o = obj(w);
  const status = STATUSES.includes(o.status as FeedbackStatus) ? o.status as FeedbackStatus : "open";
  return {
    id: str(o.id), role_key: str(o.role_key) || null, run_ref: str(o.run_ref) || null,
    kind: o.kind === "suggestion" ? "suggestion" : "complaint", target_kind: str(o.target_kind) || "other",
    target: str(o.target), body: str(o.body), evidence: arr(o.evidence).map(str).filter(Boolean), status,
    resolution: str(o.resolution) || null, resolved_by: str(o.resolved_by) || null, created_at: str(o.created_at) || null,
  };
}

export function parseGroups(w: unknown): FeedbackGroup[] {
  return arr(obj(w).groups).map(obj).map((g) => ({
    target_kind: str(g.target_kind) || "other", target: str(g.target), complaints: num(g.complaints),
    suggestions: num(g.suggestions), open: num(g.open), roles: arr(g.roles).map(str).filter(Boolean),
    latest_at: str(g.latest_at) || null, items: arr(g.items).map(parseFeedback),
  }));
}

/** "tool" → "Tool"; the words the console uses for what a report is about. */
export const TARGET_WORDS: Record<string, string> = {
  tool: "Tool", skill: "Skill", grant: "Grant", brief: "Brief", workflow: "Workflow", model: "Model",
  platform: "BOSS", other: "Other",
};

const q = (projectId: string) => `project_id=${encodeURIComponent(projectId)}`;

export const readFeedback = (projectId: string, status?: FeedbackStatus): Promise<ReadResult<FeedbackGroup[]>> =>
  aaRead(`/v1/agent-feedback?${q(projectId)}${status ? `&status=${status}` : ""}`, parseGroups);
export const setFeedbackStatus = (projectId: string, id: string, status: FeedbackStatus, resolution?: string): Promise<WriteResult> =>
  obsWrite(`/v1/agent-feedback/${encodeURIComponent(id)}`, { project_id: projectId, status, ...(resolution ? { resolution } : {}) },
           { scope: `afb:${id}`, label: status === "resolved" ? "Resolve" : status === "dismissed" ? "Dismiss" : "Acknowledge" }, "PATCH");
