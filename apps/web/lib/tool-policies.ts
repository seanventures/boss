/**
 * THE ORDERED TOOL-POLICY RULES, read and changed from the console (BOSSAPP-815/817; API
 * routers/tool_policies.py). Parsing is defensive and pure (tested on its own); every write goes
 * through obsWrite → apiWrite (one press, one effect).
 *
 * A rule: scope (org | project | agent:<role> | workflow:<id>), priority (lower runs first),
 * effect (allow | ask | deny | rate_limit), selector {server, tool} globs, conditions on the
 * call's arguments, source (operator | decision — an "Always allow" from an approval).
 */
import { aaRead, type ReadResult } from "./ambient";
import { obsWrite, type WriteResult } from "../app/observability/obs-write";

export type Effect = "allow" | "ask" | "deny" | "rate_limit";
export const EFFECTS: Effect[] = ["deny", "ask", "allow", "rate_limit"];
export const EFFECT_WORDS: Record<Effect, string> = { allow: "Allow", ask: "Ask", deny: "Deny", rate_limit: "Rate limit" };
export type Condition = { path: string; op: "equals" | "prefix" | "regex"; value: unknown };
export type Rule = {
  id: string; scope: string; priority: number; effect: Effect;
  selector: { server: string; tool: string }; conditions: Condition[];
  rate_limit: { max_calls: number; window_seconds: number } | null;
  source: "operator" | "decision"; source_decision_id: string | null;
  status: "active" | "disabled" | "revoked"; hit_count: number; last_hit_at: string | null;
  expires_at: string | null; note: string | null; schema_sha256: string | null;
};
export type Simulation = { decision: "allow" | "ask" | "deny"; matched_policy_ids: string[]; explanation: string;
  matched_rules: Rule[] };

const obj = (v: unknown): Record<string, unknown> =>
  (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown) => (typeof v === "string" ? v : "");
const effect = (v: unknown): Effect => (EFFECTS.includes(v as Effect) ? v as Effect : "ask");

export function parseRule(w: unknown): Rule {
  const o = obj(w);
  const sel = obj(o.selector);
  const rl = obj(o.rate_limit);
  return {
    id: str(o.id), scope: str(o.scope) || "project", priority: Number(o.priority) || 0, effect: effect(o.effect),
    selector: { server: str(sel.server) || "*", tool: str(sel.tool) || "*" },
    conditions: arr(o.conditions).map(obj).map((c) => ({
      path: str(c.path), op: (["equals", "prefix", "regex"].includes(str(c.op)) ? str(c.op) : "equals") as Condition["op"],
      value: c.value })),
    rate_limit: o.rate_limit ? { max_calls: Number(rl.max_calls) || 0, window_seconds: Number(rl.window_seconds) || 0 } : null,
    source: o.source === "decision" ? "decision" : "operator", source_decision_id: str(o.source_decision_id) || null,
    status: o.status === "disabled" || o.status === "revoked" ? o.status : "active",
    hit_count: Number(o.hit_count) || 0, last_hit_at: str(o.last_hit_at) || null,
    expires_at: str(o.expires_at) || null, note: str(o.note) || null, schema_sha256: str(o.schema_sha256) || null,
  };
}

export const parseRules = (w: unknown): Rule[] => arr(obj(w).policies).map(parseRule);

export function parseSimulation(w: unknown): Simulation {
  const o = obj(w);
  const d = str(o.decision);
  return { decision: d === "allow" || d === "deny" ? d : "ask", matched_policy_ids: arr(o.matched_policy_ids).map(str),
           explanation: str(o.explanation), matched_rules: arr(o.matched_rules).map(parseRule) };
}

/** "agent:forge" → "Agent forge"; "org" → "Org"; "workflow:w1" → "Workflow w1". */
export function scopeWords(scope: string): string {
  const [kind, ...rest] = scope.split(":");
  const name = rest.join(":");
  const head = kind === "agent" ? "Agent" : kind === "workflow" ? "Workflow" : kind === "org" ? "Org" : "Project";
  return name ? `${head} ${name}` : head;
}

/** One line for a rule's conditions: `$.path prefix "/srv"` joined by "and". */
export function conditionWords(c: Condition[]): string {
  return c.map((x) => `${x.path} ${x.op} ${JSON.stringify(x.value)}`).join(" and ");
}

/** The conditions box: one per line, `path op value` (value JSON, or bare text). */
export function readConditions(textValue: string): { conditions: Condition[]; error: string | null } {
  const out: Condition[] = [];
  for (const [i, line] of textValue.split("\n").map((l) => l.trim()).filter(Boolean).entries()) {
    const m = /^(\S+)\s+(equals|prefix|regex)\s+(.+)$/.exec(line);
    if (!m) return { conditions: [], error: `Line ${i + 1}: write path op value, e.g. $.path prefix /srv` };
    let value: unknown = m[3];
    try { value = JSON.parse(m[3]); } catch { /* bare text */ }
    out.push({ path: m[1], op: m[2] as Condition["op"], value });
  }
  return { conditions: out, error: null };
}

/** The rules in the order the engine reads them (priority, then scope specificity, effect, id). */
export function ordered(rules: Rule[]): Rule[] {
  const scopeRank = (s: string) => ({ workflow: 0, agent: 1, project: 2, org: 3 } as Record<string, number>)[s.split(":")[0]] ?? 9;
  const effRank = (e: Effect) => ({ deny: 0, rate_limit: 1, ask: 2, allow: 3 })[e];
  return [...rules].sort((a, b) => a.priority - b.priority || scopeRank(a.scope) - scopeRank(b.scope)
    || effRank(a.effect) - effRank(b.effect) || a.id.localeCompare(b.id));
}

const q = (projectId: string) => `project_id=${encodeURIComponent(projectId)}`;
const one = (id: string) => `/v1/tool-policies/${encodeURIComponent(id)}`;
const press = (scope: string, label: string) => ({ scope: `tp:${scope}`, label });

export const readRules = (projectId: string, source?: "operator" | "decision"): Promise<ReadResult<Rule[]>> =>
  aaRead(`/v1/tool-policies?${q(projectId)}${source ? `&source=${source}` : ""}`, parseRules);
export const createRule = (projectId: string, body: Record<string, unknown>): Promise<WriteResult> =>
  obsWrite("/v1/tool-policies", { project_id: projectId, ...body }, press("new", "Add rule"));
export const updateRule = (projectId: string, id: string, fields: Record<string, unknown>, label = "Save"): Promise<WriteResult> =>
  obsWrite(one(id), { project_id: projectId, ...fields }, press(id, label), "PATCH");
export const revokeRule = (projectId: string, id: string): Promise<WriteResult> =>
  obsWrite(`${one(id)}?${q(projectId)}`, {}, press(id, "Revoke"), "DELETE");
export const simulate = (projectId: string, body: Record<string, unknown>): Promise<WriteResult> =>
  obsWrite("/v1/tool-policies/simulate", { project_id: projectId, ...body }, press("simulate", "Simulate"));
