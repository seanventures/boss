/**
 * THE SKILL-ACCESS RULES (BOSSAPP-890; API routers/skill_policies.py), read and changed from
 * Skills & MCP › Rules. A rule: for these actions (create, edit, import, install, test, remove,
 * hold, invoke), on skills matching a glob, by a role, done by a person or an agent, from a
 * source, at or above a risk tier — allow or deny. Read in order; the first match decides; no
 * match allows. Parsing is pure; every write goes through obsWrite.
 */
import { aaRead, type ReadResult } from "./ambient";
import { obsWrite, type WriteResult } from "../app/observability/obs-write";

export const SKILL_ACTIONS = ["create", "edit", "import", "install", "test", "remove", "hold", "invoke"] as const;
export type SkillAction = typeof SKILL_ACTIONS[number];
export type SkillRule = {
  id: string; scope: "project" | "org"; position: number; effect: "allow" | "deny"; actions: string[];
  skill_pattern: string; role_pattern: string; actor_kind: "any" | "person" | "agent"; source_pattern: string;
  min_risk_tier: number | null; note: string | null; status: "active" | "disabled" | "revoked"; hit_count: number;
};
export type SkillSimulation = { decision: "allow" | "deny"; rule_id: string | null; explanation: string };

const obj = (v: unknown): Record<string, unknown> =>
  (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown) => (typeof v === "string" ? v : "");

export function parseSkillRule(w: unknown): SkillRule {
  const o = obj(w);
  const kind = str(o.actor_kind);
  const tier = typeof o.min_risk_tier === "number" ? o.min_risk_tier : null;
  return {
    id: str(o.id), scope: o.scope === "org" ? "org" : "project", position: Number(o.position) || 0,
    effect: o.effect === "allow" ? "allow" : "deny", actions: arr(o.actions).map(str).filter(Boolean),
    skill_pattern: str(o.skill_pattern) || "*", role_pattern: str(o.role_pattern) || "*",
    actor_kind: kind === "person" || kind === "agent" ? kind : "any", source_pattern: str(o.source_pattern) || "*",
    min_risk_tier: tier, note: str(o.note) || null,
    status: o.status === "disabled" || o.status === "revoked" ? o.status : "active", hit_count: Number(o.hit_count) || 0,
  };
}

export const parseSkillRules = (w: unknown): SkillRule[] => arr(obj(w).rules).map(parseSkillRule);

export function parseSkillSimulation(w: unknown): SkillSimulation {
  const o = obj(w);
  return { decision: o.decision === "deny" ? "deny" : "allow", rule_id: str(o.rule_id) || null, explanation: str(o.explanation) };
}

/** One line for who/what a rule covers: "agents · role forge · from github:acme/* · tier 2+". */
export function ruleWords(r: SkillRule): string {
  const bits: string[] = [];
  if (r.actor_kind !== "any") bits.push(r.actor_kind === "agent" ? "agents" : "people");
  if (r.role_pattern !== "*") bits.push(`role ${r.role_pattern}`);
  if (r.source_pattern !== "*") bits.push(`from ${r.source_pattern}`);
  if (r.min_risk_tier !== null) bits.push(`tier ${r.min_risk_tier}+`);
  return bits.join(" · ") || "anyone";
}

const q = (projectId: string) => `project_id=${encodeURIComponent(projectId)}`;
const one = (id: string) => `/v1/skill-policies/${encodeURIComponent(id)}`;
const press = (scope: string, label: string) => ({ scope: `skp:${scope}`, label });

export const readSkillRules = (projectId: string): Promise<ReadResult<SkillRule[]>> =>
  aaRead(`/v1/skill-policies?${q(projectId)}`, parseSkillRules);
export const createSkillRule = (projectId: string, body: Record<string, unknown>): Promise<WriteResult> =>
  obsWrite("/v1/skill-policies", { project_id: projectId, ...body }, press("new", "Add rule"));
export const updateSkillRule = (projectId: string, id: string, fields: Record<string, unknown>, label = "Save"): Promise<WriteResult> =>
  obsWrite(one(id), { project_id: projectId, ...fields }, press(id, label), "PATCH");
export const revokeSkillRule = (projectId: string, id: string): Promise<WriteResult> =>
  obsWrite(`${one(id)}?${q(projectId)}`, {}, press(id, "Revoke"), "DELETE");
export const simulateSkillRule = (projectId: string, body: Record<string, unknown>): Promise<WriteResult> =>
  obsWrite("/v1/skill-policies/simulate", { project_id: projectId, ...body }, press("simulate", "Simulate"));
