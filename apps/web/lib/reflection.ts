/**
 * THE REFLECTION AGENT (BOSSAPP-888; API routers/reflection.py): its schedule (paused by default),
 * its recent runs, and Reflect now. It only proposes — each proposal is a card in Needs you that a
 * person approves. Parsing is pure; writes go through obsWrite.
 */
import { aaRead, type ReadResult } from "./ambient";
import { obsWrite, type WriteResult } from "../app/observability/obs-write";

export type ReflectionProposal = { kind: "skill_diff" | "agent_prompt_diff"; target: string; decision_id: string;
  cited_runs: string[]; evidence: number };
export type ReflectionRun = { id: string; role_key: string; trigger: string;
  status: "proposed" | "nothing_to_propose" | "failed"; proposals: ReflectionProposal[]; error: string | null;
  created_at: string | null };
export type ReflectionState = { enabled: boolean; every_hours: number; roles: string[]; last_run_at: string | null;
  runs: ReflectionRun[] };

const obj = (v: unknown): Record<string, unknown> =>
  (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown) => (typeof v === "string" ? v : "");

export function parseReflection(w: unknown): ReflectionState {
  const o = obj(w);
  const s = obj(o.settings);
  return {
    enabled: s.enabled === true, every_hours: Number(s.every_hours) || 168, roles: arr(s.roles).map(str).filter(Boolean),
    last_run_at: str(s.last_run_at) || null,
    runs: arr(o.runs).map(obj).map((r) => ({
      id: str(r.id), role_key: str(r.role_key), trigger: str(r.trigger) || "manual",
      status: r.status === "proposed" || r.status === "failed" ? r.status : "nothing_to_propose",
      proposals: arr(r.proposals).map(obj).map((p) => ({
        kind: p.kind === "skill_diff" ? "skill_diff" : "agent_prompt_diff", target: str(p.target),
        decision_id: str(p.decision_id), cited_runs: arr(p.cited_runs).map(str).filter(Boolean),
        evidence: Number(p.evidence) || 0 }) as ReflectionProposal),
      error: str(r.error) || null, created_at: str(r.created_at) || null })),
  };
}

export const PROPOSAL_WORDS: Record<ReflectionProposal["kind"], string> = {
  skill_diff: "Skill change", agent_prompt_diff: "Instruction change",
};

const q = (projectId: string) => `project_id=${encodeURIComponent(projectId)}`;

export const readReflection = (projectId: string): Promise<ReadResult<ReflectionState>> =>
  aaRead(`/v1/reflection?${q(projectId)}`, parseReflection);
export const setReflection = (projectId: string, fields: { enabled?: boolean; every_hours?: number; roles?: string[] }): Promise<WriteResult> =>
  obsWrite("/v1/reflection/settings", { project_id: projectId, ...fields },
           { scope: "rfl:settings", label: fields.enabled === false ? "Pause" : "Turn on" }, "PUT");
export const runReflection = (projectId: string, roleKey: string): Promise<WriteResult> =>
  obsWrite("/v1/reflection/run", { project_id: projectId, role_key: roleKey }, { scope: `rfl:${roleKey}`, label: "Reflect now" });
