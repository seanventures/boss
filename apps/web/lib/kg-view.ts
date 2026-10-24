/**
 * THE GRAPH AREA'S CALLS (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §3, §5): one door for every read and
 * write the area makes, in the shapes of lib/kg-view-types.ts (the Phase 0 contract).
 *
 * Reads go to the same-origin `/v1` proxy (app/v1/[...path]) and answer `{ok, data}` or
 * `{ok:false, status, error}` with the API's own sentence (lib/api-error.ts), so every panel can
 * say what went wrong in words and keep the other panels working (§5.8 "error").
 *
 * Writes go through lib/api-write.ts (one press, one effect) and are registered with the
 * permission their route checks in lib/identity/write-permissions.ts; the screens pre-grey them
 * (components/gated.tsx).
 */
import { apiErrorText } from "./api-error";
import { apiWrite } from "./api-write";
import { API_URL } from "./client";
import { repoUrl } from "./github-urls";
import type { Permission } from "./identity/permissions.gen";
import type {
  KgLiveResponse, KgNeighbourhoodResponse, KgNodeDetailResponse, KgOverviewResponse, KgRuleActionRequest,
  KgRuleActionResponse, KgRuleCategory, KgRuleScope, KgRulesResponse, KgRuleStatus, KgSearchResponse,
  KgTreeChildrenResponse, KgTreeTab,
} from "./kg-view-types";
import { KG_VIEW_BASE } from "./kg-view-types";

/** Rule review (§3, §5.7). The operator's decision: owners and admins only. `knowledge:review`
 *  (S2a) is held by exactly those roles; until it is in the generated catalogue the console
 *  mirrors the same people through `knowledge:manage`. One constant, so the switch is one line. */
export const RULE_REVIEW: Permission = "knowledge:manage";
/** Promote, reject and forget a fact (§3 "Forget/promote facts: stays knowledge:manage"). */
export const FACT_MANAGE: Permission = "knowledge:manage";
/** Index now / Rebuild: a read-safe action (apps/api knowledge.py: whoever may read may refresh). */
export const INDEX_NOW: Permission = "knowledge:read";
/** "Scan for rules here" starts the business_rules_scan workflow. */
export const SCAN_START: Permission = "workflow_run:start";

export type Read<T> = { ok: true; data: T } | { ok: false; status: number; error: string };

function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const out = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    out.set(k, typeof v === "boolean" ? (v ? "1" : "0") : String(v));
  }
  return out.toString();
}

/** One GET, said in words when it fails. `status` 0 means nothing answered. */
export async function kgRead<T>(path: string, params: Record<string, string | number | boolean | null | undefined>, signal?: AbortSignal): Promise<Read<T>> {
  try {
    const r = await fetch(`${API_URL}${path}?${qs(params)}`, { cache: "no-store", signal });
    let body: unknown = null;
    try { body = await r.json(); } catch { body = null; }
    if (!r.ok) return { ok: false, status: r.status, error: apiErrorText(body, r.status) };
    return { ok: true, data: body as T };
  } catch (e) {
    if ((e as { name?: string })?.name === "AbortError") return { ok: false, status: -1, error: "cancelled" };
    return { ok: false, status: 0, error: "BOSS did not answer. Check that the API is running, then retry." };
  }
}

type Who = { projectId: string; actor: string };

export function getOverview(w: Who, o: { layers?: string; repo?: string | null; area?: string | null } = {}, signal?: AbortSignal) {
  return kgRead<KgOverviewResponse>(`${KG_VIEW_BASE}/overview`, { project_id: w.projectId, actor: w.actor, layers: o.layers, repo: o.repo, area: o.area }, signal);
}

export function getNeighbourhood(w: Who, node: string, o: { depth: number; layers?: string; limit?: number }, signal?: AbortSignal) {
  return kgRead<KgNeighbourhoodResponse>(`${KG_VIEW_BASE}/neighbourhood`, { project_id: w.projectId, actor: w.actor, node, depth: o.depth, layers: o.layers, limit: o.limit }, signal);
}

export function getNode(w: Who, id: string, page?: { edge: string; direction: "out" | "in"; offset: number }, signal?: AbortSignal) {
  return kgRead<KgNodeDetailResponse>(`${KG_VIEW_BASE}/node/${encodeURIComponent(id)}`, { project_id: w.projectId, actor: w.actor, ...(page ?? {}) }, signal);
}

export function getTreeChildren(w: Who, o: { tab: KgTreeTab; parent?: string | null; q?: string; offset?: number }, signal?: AbortSignal) {
  return kgRead<KgTreeChildrenResponse>(`${KG_VIEW_BASE}/tree/children`, { project_id: w.projectId, actor: w.actor, tab: o.tab, parent: o.parent, q: o.q, offset: o.offset }, signal);
}

export function getSearch(w: Who, q: string, o: { layers?: string; limit?: number; meaning?: boolean } = {}, signal?: AbortSignal) {
  return kgRead<KgSearchResponse>(`${KG_VIEW_BASE}/search`, { project_id: w.projectId, actor: w.actor, q, layers: o.layers, limit: o.limit ?? 20, meaning: o.meaning ? 1 : undefined }, signal);
}

export function getRules(w: Who, o: { status?: KgRuleStatus | null; scan_id?: string | null; area?: string | null } = {}, signal?: AbortSignal) {
  return kgRead<KgRulesResponse>(`${KG_VIEW_BASE}/rules`, { project_id: w.projectId, actor: w.actor, status: o.status, scan_id: o.scan_id, area: o.area }, signal);
}

/** The live feed, read on other areas' screens too (the Learned-so-far strip): a partial or
 *  unexpected body is read as "nothing new", never a crash of the cockpit around it. */
export async function getLive(w: Who, o: { since?: string | null; run?: string | null } = {}, signal?: AbortSignal): Promise<Read<KgLiveResponse>> {
  const r = await kgRead<Partial<KgLiveResponse>>(`${KG_VIEW_BASE}/live`, { project_id: w.projectId, actor: w.actor, since: o.since, run: o.run }, signal);
  if (!r.ok) return r;
  const d = (r.data && typeof r.data === "object" ? r.data : {}) as Partial<KgLiveResponse>;
  return {
    ok: true,
    data: {
      cursor: typeof d.cursor === "string" ? d.cursor : (o.since ?? ""),
      learnings: Array.isArray(d.learnings) ? d.learnings : [],
      rules: Array.isArray(d.rules) ? d.rules : [],
      edges: Array.isArray(d.edges) ? d.edges : [],
      active_runs: typeof d.active_runs === "number" ? d.active_runs : 0,
      poll_after_ms: typeof d.poll_after_ms === "number" ? d.poll_after_ms : 5000,
    },
  };
}

/** The code graph of each registered repository (the indexing state, §5.8). */
export type KgRepository = {
  repo: string;
  branch?: string | null;
  status: string;
  summary?: string;
  error?: string | null;
  files?: number | null;
  parsed?: number | null;
  indexed_sha?: string | null;
  indexed_at?: string | null;
  graph?: { nodes: number; edges: number };
};

export function getRepositories(w: Who, signal?: AbortSignal) {
  return kgRead<{ project_id: string; repositories: KgRepository[] }>("/v1/knowledge/repositories", { project_id: w.projectId, actor: w.actor }, signal);
}

/** GET /v1/knowledge/graph-status: settings, the fact promotion queue, and (when the add-on port
 *  answers) the Rules entitlement. */
export type KgGraphStatus = {
  agent_access?: string;
  retention_years?: number;
  embedding_model?: { provider: string; model: string | null; dimension: number | null; model_id: string; check: string } | null;
  retrieval?: { mode: string; reason?: string };
  vector_index?: string;
  budget_category?: string;
  ship_gate?: string;
  cost_usd?: number | null;
  sources?: string[];
  repositories?: KgRepository[];
  promotion_candidates?: Array<{ id: string; node_key: string; body: string }>;
  /** §3.7: the entitlement port's answer for `knowledge.rules`. Absent = Community (allowed). */
  entitlements?: Record<string, { allowed: boolean; reason?: string | null }>;
};

export function getGraphStatus(w: Who, signal?: AbortSignal) {
  return kgRead<KgGraphStatus>("/v1/knowledge/graph-status", { project_id: w.projectId, actor: w.actor }, signal);
}

/** The Rules/Learning add-on (§3.7): locked only when the port says so, with its reason. */
export function rulesLock(status: KgGraphStatus | null | undefined): string | null {
  const e = status?.entitlements?.["knowledge.rules"];
  if (!e || e.allowed) return null;
  return e.reason || "Rules and Learning are part of the Knowledge add-on, which this install does not include.";
}

/** Parity with the old explorer (§5.1): "why" a node is believed, and its blast radius. */
export function getWhy(w: Who, nodeId: string, signal?: AbortSignal) {
  return kgRead<{ why?: Array<{ name: string; body: string; trust_tier: string }> }>(`/v1/knowledge/nodes/${encodeURIComponent(nodeId)}`, { project_id: w.projectId, actor: w.actor }, signal);
}

export function getBlastRadius(w: Who, nodeId: string, signal?: AbortSignal) {
  return kgRead<{ affected?: Array<{ id?: string; node_id?: string; name?: string; kind?: string }> }>("/v1/knowledge/blast-radius", { project_id: w.projectId, actor: w.actor, node_id: nodeId }, signal);
}

// ── writes ──────────────────────────────────────────────────────────────────────────────────

export type Wrote<T> = { ok: true; data: T } | { ok: false; status: number; error: string };

async function send<T>(path: string, body: Record<string, unknown>, press: { scope: string; label: string }): Promise<Wrote<T>> {
  try {
    const r = await apiWrite(`${API_URL}${path}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), cache: "no-store",
    }, { press });
    let parsed: unknown = null;
    try { parsed = await r.json(); } catch { parsed = null; }
    if (!r.ok) return { ok: false, status: r.status, error: apiErrorText(parsed, r.status) };
    return { ok: true, data: parsed as T };
  } catch (e) {
    return { ok: false, status: 0, error: e instanceof Error ? e.message : String(e) };
  }
}

type RuleEdit = { title?: string; statement?: string; category?: KgRuleCategory; scope?: Partial<KgRuleScope>; note?: string };

function ruleBody(w: Who, extra: RuleEdit = {}): KgRuleActionRequest {
  return { project_id: w.projectId, actor: w.actor, step_up: "confirmed", ...extra };
}

export async function approveRule(w: Who, ruleId: string, note?: string) {
  return send<KgRuleActionResponse>(`${KG_VIEW_BASE}/rules/${encodeURIComponent(ruleId)}/approve`, ruleBody(w, note ? { note } : {}), { scope: `kg-rule:${ruleId}`, label: "Approve" });
}

export async function editRule(w: Who, ruleId: string, edit: RuleEdit) {
  return send<KgRuleActionResponse>(`${KG_VIEW_BASE}/rules/${encodeURIComponent(ruleId)}/edit`, ruleBody(w, edit), { scope: `kg-rule:${ruleId}`, label: "Save and approve" });
}

export async function rejectRule(w: Who, ruleId: string, note?: string) {
  return send<KgRuleActionResponse>(`${KG_VIEW_BASE}/rules/${encodeURIComponent(ruleId)}/reject`, ruleBody(w, note ? { note } : {}), { scope: `kg-rule:${ruleId}`, label: "Reject" });
}

/** Index now / Rebuild: re-read the repository's branch into the graph (no model). */
export async function indexNow(w: Who, repository = "") {
  return send<{ repositories?: KgRepository[]; message?: string }>("/v1/knowledge/repositories/index",
    { project_id: w.projectId, actor: w.actor, repository, force: true }, { scope: `kg-index:${repository || "all"}`, label: "Index now" });
}

export async function promoteFact(w: Who, factId: string) {
  return send<Record<string, unknown>>(`/v1/knowledge/facts/${encodeURIComponent(factId)}/promote`,
    { project_id: w.projectId, actor: w.actor, step_up: "confirmed" }, { scope: `kg-fact:${factId}`, label: "Approve fact" });
}

export async function rejectFact(w: Who, factId: string) {
  return send<Record<string, unknown>>(`/v1/knowledge/facts/${encodeURIComponent(factId)}/reject`,
    { project_id: w.projectId, actor: w.actor, step_up: "confirmed" }, { scope: `kg-fact:${factId}`, label: "Reject fact" });
}

export async function forgetFact(w: Who, factId: string) {
  return send<Record<string, unknown>>(`/v1/knowledge/facts/${encodeURIComponent(factId)}/forget`,
    { project_id: w.projectId, actor: w.actor, step_up: "confirmed" }, { scope: `kg-fact:${factId}`, label: "Forget" });
}

// ── links ───────────────────────────────────────────────────────────────────────────────────

/** Open the Graph focused on a node (the learned strip, a rule's evidence, a decision card). */
export function kgNodeHref(nodeId: string, orgId?: string | null): string {
  const p = new URLSearchParams();
  if (orgId) p.set("org", orgId);
  p.set("area", "knowledge");
  p.set("kg_node", nodeId);
  return `/?${p.toString()}`;
}

/** Graph › Rules review, filtered by a scan (the scan's decision card, §4.5). */
export function kgRulesHref(o: { scanId?: string | null; sub?: "facts" | null } = {}, orgId?: string | null): string {
  const p = new URLSearchParams();
  if (orgId) p.set("org", orgId);
  p.set("area", "knowledge");
  p.set("kg_tab", "rules");
  if (o.scanId) p.set("kg_scan", o.scanId);
  if (o.sub) p.set("kg_sub", o.sub);
  return `/?${p.toString()}`;
}

/** A run, where it lives: a build (code task run) in Factory, a workflow run in Workflows. */
export function kgRunHref(prov: { code_task_run_id?: string | null; workflow_run_id?: string | null }, orgId?: string | null): string | null {
  const p = new URLSearchParams();
  if (orgId) p.set("org", orgId);
  if (prov.code_task_run_id) { p.set("area", "code_tasks"); p.set("build", prov.code_task_run_id); return `/?${p.toString()}`; }
  if (prov.workflow_run_id) { p.set("area", "delivery_workflows"); p.set("wfr", prov.workflow_run_id); return `/?${p.toString()}`; }
  return null;
}

/** The code on GitHub at those lines, through the shared URL builder (lib/github-urls.ts). */
export function codeHref(repo: string | null | undefined, path: string | null | undefined, lines?: [number, number] | number | null, ref = "HEAD"): string | null {
  if (!repo || !path || !/^[\w.-]+\/[\w.-]+$/.test(repo)) return null;
  const clean = path.split("/").map(encodeURIComponent).join("/");
  const anchor = Array.isArray(lines) ? `#L${lines[0]}-L${lines[1]}` : typeof lines === "number" ? `#L${lines}` : "";
  return `${repoUrl(repo)}/blob/${encodeURIComponent(ref)}/${clean}${anchor}`;
}

/** The layers param for a chip set: All = every layer. */
export function layersParam(chips: ReadonlySet<string>): string {
  return chips.size ? [...chips].join(",") : "code,rules,learning";
}
