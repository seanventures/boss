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
import { localWith } from "./local-time";
import type { Permission } from "./identity/permissions.gen";
import type {
  KgLiveResponse, KgNeighbourhoodResponse, KgNodeDetailResponse, KgOverviewResponse, KgRuleActionRequest,
  KgRuleActionResponse, KgRuleCategory, KgRuleScope, KgRulesResponse, KgRuleStatus, KgSearchResponse,
  KgTreeChildrenResponse, KgTreeTab, KgRunProofResponse,
} from "./kg-view-types";
import { KG_VIEW_BASE } from "./kg-view-types";

/** Rule review (§3, §5.7). The operator's decision: owners and admins only — exactly the holders
 *  of `knowledge:review` (step-up, never delegable: no agent run can hold it). */
export const RULE_REVIEW: Permission = "knowledge:review";
/** Promote, reject and forget a fact (§3 "Forget/promote facts: stays knowledge:manage"). */
export const FACT_MANAGE: Permission = "knowledge:manage";
/** Index now / Rebuild: a read-safe action (apps/api knowledge.py: whoever may read may refresh). */
export const INDEX_NOW: Permission = "knowledge:read";
/** "Scan for rules here" starts the business_rules_scan workflow. */
export const SCAN_START: Permission = "workflow_run:start";

export type Read<T> = { ok: true; data: T } | { ok: false; status: number; error: string; unavailable?: boolean };

/** What a screen says when its route is not on this install yet (tester round 1 P0-3): never the
 *  framework's bare "Not Found". */
export const UNAVAILABLE = "This part of the graph is not on this install yet. It arrives with the release's knowledge update.";

/** A 404 from a route that does not exist (FastAPI's own "Not Found"), as opposed to a route
 *  answering that one thing is missing ("no such node"). */
export function isMissingRoute(status: number, body: unknown): boolean {
  if (status !== 404 && status !== 405) return false;
  const d = body && typeof body === "object" ? (body as { detail?: unknown }).detail : body;
  return status === 405 || d === "Not Found" || d === undefined || d === null;
}

function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const out = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    out.set(k, typeof v === "boolean" ? (v ? "1" : "0") : String(v));
  }
  return out.toString();
}

/** One GET, said in words when it fails. `status` 0 means nothing answered. */
/** A cold console (tester round 2): the API or its identity check still starting. Retried with
 *  backoff before anything is said; then said in words, never "identity_unavailable". */
export const STARTING = "BOSS is still starting up. It will answer in a moment — Retry, or wait a few seconds.";
export const RETRY_DELAYS_MS = [700, 1600, 3200];

function warmingUp(status: number, body: unknown): boolean {
  if (status === 0 || status === 502 || status === 504) return true;
  if (status !== 503) return false;
  const text = JSON.stringify(body ?? "").toLowerCase();
  return /identity_unavailable|starting|warming|not ready|unavailable/.test(text);
}

const sleep = (ms: number, signal?: AbortSignal) => new Promise<void>((resolve) => {
  const t = setTimeout(resolve, ms);
  signal?.addEventListener?.("abort", () => { clearTimeout(t); resolve(); });
});

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** The time the engine writes into a run's or a scan's name: " · Oct 25, 07:13", always UTC. */
const BAKED_UTC = /^(.*) · (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{1,2}), (\d{2}):(\d{2})$/;

/**
 * ONE ZONE ON EVERY KG SCREEN (tester round 5: run names read "Oct 25, 07:13" — UTC, unlabelled —
 * beside provenance in "Oct 25, 00:13 PDT"). The engine names a run or a scan with the UTC time it
 * started; that time is re-said by kgWhen, in the viewer's zone and labelled, the moment a
 * response arrives — so the canvas, the tree, the panel and search all show the same clock.
 */
export function localizeName(name: string, now = Date.now()): string {
  const m = BAKED_UTC.exec(name);
  if (!m) return name;
  const year = new Date(now).getUTCFullYear();
  const at = (y: number) => Date.UTC(y, MONTHS.indexOf(m[2]), Number(m[3]), Number(m[4]), Number(m[5]));
  // No year in the name: this year, unless that is more than a day ahead (then last year's).
  const t = at(year) > now + 86_400_000 ? at(year - 1) : at(year);
  return `${m[1]} · ${kgWhen(new Date(t).toISOString())}`;
}

/** localizeName over a response: every `label`, `name` and `title` string, at any depth. */
export function localizeNames<T>(body: T): T {
  const walk = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(walk);
    if (!v || typeof v !== "object") return v;
    const out: Record<string, unknown> = {};
    for (const [k, x] of Object.entries(v as Record<string, unknown>)) {
      out[k] = typeof x === "string" && (k === "label" || k === "name" || k === "title") ? localizeName(x) : walk(x);
    }
    return out;
  };
  return walk(body) as T;
}

/** One GET, said in words when it fails. `status` 0 means nothing answered. */
export async function kgRead<T>(path: string, params: Record<string, string | number | boolean | null | undefined>, signal?: AbortSignal): Promise<Read<T>> {
  for (let attempt = 0; ; attempt += 1) {
    let status = 0;
    let body: unknown = null;
    try {
      const r = await fetch(`${API_URL}${path}?${qs(params)}`, { cache: "no-store", signal });
      status = r.status;
      try { body = await r.json(); } catch { body = null; }
      if (r.ok) return { ok: true, data: localizeNames(body) as T };
      if (isMissingRoute(r.status, body)) return { ok: false, status: r.status, error: UNAVAILABLE, unavailable: true };
    } catch (e) {
      if ((e as { name?: string })?.name === "AbortError") return { ok: false, status: -1, error: "cancelled" };
      status = 0;
    }
    if (warmingUp(status, body) && attempt < RETRY_DELAYS_MS.length && !signal?.aborted) {
      await sleep(RETRY_DELAYS_MS[attempt], signal);
      if (signal?.aborted) return { ok: false, status: -1, error: "cancelled" };
      continue;
    }
    if (status === 0) return { ok: false, status: 0, error: "BOSS did not answer. Check that the API is running, then retry." };
    if (warmingUp(status, body)) return { ok: false, status, error: STARTING };
    return { ok: false, status, error: apiErrorText(body, status) };
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

/** The reuse scorecard (§4.6): one run (or conversation), or it against a baseline — what it
 *  carried from the baseline, each metric's change and BOSS's verdict. */
export function getProof(w: Who, run: string, baseline?: string | null, signal?: AbortSignal) {
  return kgRead<KgRunProofResponse>(`${KG_VIEW_BASE}/proof`, { project_id: w.projectId, actor: w.actor, run, baseline }, signal);
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

/** A time in the viewer's own zone, labelled with it: "Oct 24, 09:40 PDT" (tester round 1 P1-12:
 *  one formatter for the whole area; never local time labelled UTC). */
export function kgWhen(at: string | null | undefined): string {
  if (!at) return "";
  const t = Date.parse(at);
  if (Number.isNaN(t)) return at;
  return localWith(t, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false, timeZoneName: "short" });
}

/** Search hits that share a name told apart (P1-9): the part of each path the others do not
 *  share, middle-ellipsised, so `…/loanaccount/service/Foo.java` and `…/savings/service/Foo.java`
 *  never read the same. */
export function distinctPaths(paths: Array<string | null | undefined>, max = 44): string[] {
  const split = paths.map((p) => (p ?? "").split("/").filter(Boolean));
  return split.map((segs, i) => {
    if (!segs.length) return "";
    const twins = split.filter((o, j) => j !== i && o[o.length - 1] === segs[segs.length - 1]);
    // Keep the file and the first segment from the end that differs from every twin's.
    let from = segs.length - 1;
    for (const o of twins) {
      let k = 1;
      while (k <= segs.length && k <= o.length && segs[segs.length - k] === o[o.length - k]) k += 1;
      from = Math.min(from, segs.length - k);
    }
    const file = segs[segs.length - 1];
    const parts: string[] = [segs[0]];
    const diff = Math.min(from, segs.length - 2);
    if (diff > 1) parts.push("…");
    if (diff >= 1) parts.push(segs[diff]);
    if (diff >= 1 && diff < segs.length - 2) parts.push(segs.length - 2 - diff > 1 ? "…" : segs[segs.length - 2]);
    if (segs.length > 1) parts.push(file);
    let text = parts.join("/");
    // Too long: the area goes first; the segment that tells them apart never does.
    if (text.length > max && parts.length > 2) text = ["…", ...parts.slice(parts[1] === "…" ? 2 : 1)].join("/");
    if (text.length <= max) return text;
    const keep = max - 1;
    return `${text.slice(0, Math.ceil(keep / 3))}…${text.slice(text.length - (keep - Math.ceil(keep / 3)))}`;
  });
}

/** A type-like query (it starts upper-case: "LoanTransaction") ranks types above members within
 *  the server's own tiers (P1-9); a member-like one keeps the server's order. */
export function rankHits<H extends { match: string; node: { symbol_kind?: string } }>(q: string, hits: H[]): H[] {
  if (!/^[A-Z]/.test(q.trim())) return hits;
  const tier: Record<string, number> = { exact: 0, prefix: 1, text: 2, meaning: 3 };
  // Types first, then files and modules, then members (tester round 2: modules ranked above classes).
  const member = (h: H) => {
    const k = h.node.symbol_kind ?? (h.node as { kind?: string }).kind ?? "";
    return k === "method" || k === "function" ? 2 : k === "module" || k === "file" ? 1 : 0;
  };
  return hits.map((h, i) => ({ h, i })).sort((a, b) => (tier[a.h.match] ?? 9) - (tier[b.h.match] ?? 9)
    || member(a.h) - member(b.h) || a.i - b.i).map((x) => x.h);
}


/**
 * A FOCUS'S COUNTS, ONE SOURCE (tester round 4: "and 40 connected within 1 hop" beside "Showing 40
 * of 47", "80 connected" beside "Showing 80 of 809", "200 is the most one view reads" on 147).
 * The header, the truncation banner and the panel's "In this view" all read these numbers:
 * `total` connected within the hops (what was read plus what the limit left out), `shown` what was
 * read, `drawn` the neighbours with a circle of their own, and `grouped` in `groups` bubbles.
 */
export type FocusCounts = { total: number; shown: number; drawn: number; grouped: number; groups: number; noun: string; maxed: boolean };

export function focusCounts(
  h: { center: { id: string }; nodes: Array<{ id: string }>; hidden: { nodes: number }; level: string },
  picture: { nodes: Array<{ id: string; kind: string }>; groups: Map<string, unknown[]> } | null,
  opts: { max: number; asked: number },
): FocusCounts {
  const shown = h.nodes.filter((n) => n.id !== h.center.id).length;
  const total = shown + Math.max(0, h.hidden.nodes);
  const groups = picture ? picture.groups.size : 0;
  const grouped = picture ? [...picture.groups.values()].reduce((s, m) => s + m.length, 0) : 0;
  const drawn = picture ? picture.nodes.filter((n) => n.id !== h.center.id && n.kind !== "group").length : shown;
  const noun = h.level === "class" ? "classes" : h.level === "package" ? "packages" : h.level === "method" ? "symbols" : "nodes";
  // "The most one view reads" only when that is why: asked for the most, and read the most.
  const maxed = opts.asked >= opts.max && shown >= opts.max;
  return { total, shown, drawn, grouped, groups, noun, maxed };
}

/** "AbstractLoan… · 47 connected within 1 hop · the 40 most connected shown · 31 drawn, 9 in 3 groups". */
export function focusNote(label: string, depth: number, c: FocusCounts): string {
  const parts = [`${label} · ${c.total.toLocaleString("en-US")} connected within ${depth} ${depth === 1 ? "hop" : "hops"}`];
  if (c.shown < c.total) parts.push(`the ${c.shown.toLocaleString("en-US")} most connected shown`);
  if (c.groups) parts.push(`${c.drawn.toLocaleString("en-US")} drawn, ${c.grouped.toLocaleString("en-US")} in ${c.groups} ${c.groups === 1 ? "group" : "groups"}`);
  return parts.join(" · ");
}
