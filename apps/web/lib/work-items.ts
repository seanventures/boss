/**
 * THE ONE WORK-ITEM GRAPH, read and changed from the console (BOSSAPP-830/831; API
 * routers/work_items.py). Parsing is defensive and pure (tested on its own); every write goes
 * through obsWrite → apiWrite (one press, one effect).
 */
import { aaRead, type ReadResult } from "./ambient";
import { obsWrite, type WriteResult } from "../app/observability/obs-write";

export type WorkStatus = "backlog" | "todo" | "active" | "blocked" | "in_review" | "done" | "cancelled";
export const WORK_STATUSES: WorkStatus[] = ["backlog", "todo", "active", "blocked", "in_review", "done", "cancelled"];
/** Few words per status, the chip's text. */
export const STATUS_WORDS: Record<WorkStatus, string> = {
  backlog: "Backlog", todo: "To do", active: "In progress", blocked: "Blocked",
  in_review: "In review", done: "Done", cancelled: "Cancelled",
};

export type ItemRef = { id: string; human_key: string | null; title: string; status: WorkStatus; private?: boolean };
export type Stage = { kind: "review" | "approval"; participants: string[]; comment_required: boolean; max_rounds: number };
export type Policy = { stages: Stage[] };
export type StageEvent = { stage_index: number; stage_kind: string; round: number; event: string;
  decision_id: string | null; actor: string; comment: string | null; created_at: string | null };
export type WorkNode = {
  id: string; human_key: string | null; title: string; description: string; status: WorkStatus;
  parent_id: string | null; goal_id: string | null; assignee: string | null;
  responsible_person: string | null; subject_type: string;
  labels: { name: string; color: string }[]; blocked_by: ItemRef[];
  execution_policy: Policy | null; execution_state: Record<string, unknown> | null;
  children: WorkNode[];
  /** BOSSAPP-876: private to its people (and everything under it). */
  visibility: "project" | "private";
  /** BOSSAPP-878: under an active hold of its work tree (tree rows). */
  on_hold?: boolean;
  // detail only
  relations?: { kind: string; item: ItemRef }[];
  runs?: { kind: string; id: string; status: string }[];
  stages?: StageEvent[];
  restricted?: boolean;
  access?: { party: string; granted_by: string }[];
  break_glass_reads?: { actor: string; reason: string; at: string | null }[];
  /** BOSSAPP-878: the active hold this item is under (detail). */
  hold?: Hold | null;
};
export type Hold = { id: string; root_kind: string; root_id: string; mode: "pause" | "drain";
  status: string; reason: string; created_by: string; created_at: string | null };
export type HoldPlanMember = { member_kind: string; member_id: string; action?: string; restored_action?: string };

export function parseHold(w: unknown): Hold | null {
  const o = obj(w);
  if (!str(o.id)) return null;
  return { id: str(o.id), root_kind: str(o.root_kind), root_id: str(o.root_id), mode: o.mode === "drain" ? "drain" : "pause",
    status: str(o.status), reason: str(o.reason), created_by: str(o.created_by), created_at: str(o.created_at) || null };
}

/** "Pauses 2 runs · holds 3 items": a hold plan (or a restore plan) in few words. */
export function planWords(members: HoldPlanMember[], restore = false): string {
  const count = (kind: string, act: string) => members.filter((m) => m.member_kind === kind
    && (restore ? m.restored_action : m.action) === act).length;
  const parts = restore
    ? [[count("workflow_run", "resumed"), "resumes", "run"], [count("workflow_run", "left_paused"), "leaves paused", "run"]]
    : [[count("workflow_run", "paused"), "pauses", "run"], [count("workflow_run", "left_running"), "lets finish", "run"],
       [count("work_item", "held"), "holds", "item"]];
  const said = parts.filter(([n]) => Number(n) > 0).map(([n, verb, noun]) => `${verb} ${n} ${noun}${Number(n) > 1 ? "s" : ""}`);
  return said.length ? said.join(" · ") : "nothing to change";
}
export type WorkTree = { items: WorkNode[]; count: number; by_status: Record<string, number>;
  goals: { id: string; title: string; status: string; items: number }[];
  labels: { name: string; color: string; items: number }[] };

const obj = (v: unknown): Record<string, unknown> =>
  (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown) => (typeof v === "string" ? v : "");
const status = (v: unknown): WorkStatus => (WORK_STATUSES.includes(v as WorkStatus) ? v as WorkStatus : "active");

const ref = (w: unknown): ItemRef => {
  const o = obj(w);
  return { id: str(o.id), human_key: str(o.human_key) || null, title: str(o.title), status: status(o.status),
    ...(o.private === true ? { private: true } : {}) };
};

export function parseNode(w: unknown): WorkNode {
  const o = obj(w);
  const policy = obj(o.execution_policy);
  return {
    id: str(o.id), human_key: str(o.human_key) || null, title: str(o.title) || "(untitled)",
    description: str(o.description), status: status(o.status),
    parent_id: str(o.parent_id) || null, goal_id: str(o.goal_id) || null,
    assignee: str(o.assignee) || null, responsible_person: str(o.responsible_person) || null,
    subject_type: str(o.subject_type) || "item",
    labels: arr(o.labels).map(obj).map((l) => ({ name: str(l.name), color: str(l.color) || "gray" })),
    blocked_by: arr(o.blocked_by).map(ref),
    execution_policy: Array.isArray(policy.stages) ? policy as unknown as Policy : null,
    execution_state: Object.keys(obj(o.execution_state)).length ? obj(o.execution_state) : null,
    children: arr(o.children).map(parseNode),
    visibility: o.visibility === "private" ? "private" : "project",
    ...(typeof o.on_hold === "boolean" ? { on_hold: o.on_hold } : {}),
    ...(o.hold !== undefined ? { hold: parseHold(o.hold) } : {}),
    ...(typeof o.restricted === "boolean" ? { restricted: o.restricted } : {}),
    ...(Array.isArray(o.access) ? { access: arr(o.access).map(obj).map((g) => ({ party: str(g.party), granted_by: str(g.granted_by) })) } : {}),
    ...(Array.isArray(o.break_glass_reads) ? { break_glass_reads: arr(o.break_glass_reads).map(obj).map((b) => ({
      actor: str(b.actor), reason: str(b.reason), at: str(b.at) || null })) } : {}),
    ...(Array.isArray(o.relations) ? { relations: arr(o.relations).map(obj).map((r) => ({ kind: str(r.kind), item: ref(r.item) })) } : {}),
    ...(Array.isArray(o.runs) ? { runs: arr(o.runs).map(obj).map((r) => ({ kind: str(r.kind), id: str(r.id), status: str(r.status) })) } : {}),
    ...(Array.isArray(o.stages) ? { stages: arr(o.stages).map(obj).map((s) => ({
      stage_index: Number(s.stage_index) || 0, stage_kind: str(s.stage_kind), round: Number(s.round) || 1,
      event: str(s.event), decision_id: str(s.decision_id) || null, actor: str(s.actor),
      comment: str(s.comment) || null, created_at: str(s.created_at) || null })) } : {}),
  };
}

export function parseTree(w: unknown): WorkTree {
  const o = obj(w);
  return {
    items: arr(o.items).map(parseNode), count: Number(o.count) || 0,
    by_status: Object.fromEntries(Object.entries(obj(o.by_status)).map(([k, v]) => [k, Number(v) || 0])),
    goals: arr(o.goals).map(obj).map((g) => ({ id: str(g.id), title: str(g.title), status: str(g.status), items: Number(g.items) || 0 })),
    labels: arr(o.labels).map(obj).map((l) => ({ name: str(l.name), color: str(l.color) || "gray", items: Number(l.items) || 0 })),
  };
}

/** Every node, depth-first, with its depth — what the tree draws as rows. */
export function flatten(nodes: WorkNode[], depth = 0): { node: WorkNode; depth: number }[] {
  return nodes.flatMap((n) => [{ node: n, depth }, ...flatten(n.children, depth + 1)]);
}

/** "agent:frontend" → "frontend"; "person:ceo" → "ceo". */
export function partyName(p: string | null): string {
  return p ? p.split(":").slice(1).join(":") || p : "";
}

/** A policy typed as JSON in the editor: parsed, or the reason it cannot be used. */
export function readPolicy(textValue: string): { policy: Policy | null; error: string | null } {
  if (!textValue.trim()) return { policy: null, error: null };
  let raw: unknown;
  try { raw = JSON.parse(textValue); } catch { return { policy: null, error: "Not valid JSON" }; }
  const stages = Array.isArray(raw) ? raw : obj(raw).stages;
  if (!Array.isArray(stages)) return { policy: null, error: "Needs {\"stages\": [...]}" };
  for (const [i, s] of stages.map(obj).entries()) {
    if (s.kind !== "review" && s.kind !== "approval") return { policy: null, error: `Stage ${i + 1}: kind is review or approval` };
    if (!arr(s.participants).length) return { policy: null, error: `Stage ${i + 1}: name a participant` };
  }
  return { policy: { stages: stages as Stage[] }, error: null };
}

export const POLICY_EXAMPLE = JSON.stringify({ stages: [
  { kind: "review", participants: ["agent:qa_lead"], comment_required: false, max_rounds: 3 },
  { kind: "approval", participants: ["person:ceo"], comment_required: true },
] }, null, 2);

const q = (projectId: string) => `project_id=${encodeURIComponent(projectId)}`;
const item = (id: string) => `/v1/work-items/${encodeURIComponent(id)}`;

export const readTree = (projectId: string, label?: string): Promise<ReadResult<WorkTree>> =>
  aaRead(`/v1/work-items/tree?${q(projectId)}${label ? `&label=${encodeURIComponent(label)}` : ""}`, parseTree);
export const readItem = (projectId: string, id: string): Promise<ReadResult<WorkNode>> =>
  aaRead(`${item(id)}?${q(projectId)}`, (w) => parseNode(obj(w).item));

const press = (scope: string, label: string) => ({ scope: `wi:${scope}`, label });
export const createItem = (projectId: string, body: { title: string; parent_id?: string | null }): Promise<WriteResult> =>
  obsWrite("/v1/work-items", { project_id: projectId, ...body }, press("new", "Add"));
export const updateItem = (projectId: string, id: string, fields: Record<string, unknown>, label = "Save"): Promise<WriteResult> =>
  obsWrite(item(id), { project_id: projectId, ...fields }, press(id, label), "PATCH");
export const completeItem = (projectId: string, id: string): Promise<WriteResult> =>
  obsWrite(`${item(id)}/complete`, { project_id: projectId }, press(id, "Mark done"));
export const relateItem = (projectId: string, id: string, kind: string, otherId: string): Promise<WriteResult> =>
  obsWrite(`${item(id)}/relations`, { project_id: projectId, kind, other_id: otherId }, press(id, "Link"));

/** BOSSAPP-876: who may read a private item, and the one audited read past its privacy. */
export const grantAccess = (projectId: string, id: string, party: string, revoke = false): Promise<WriteResult> =>
  obsWrite(`${item(id)}/access`, { project_id: projectId, party, revoke }, press(id, revoke ? "Remove access" : "Grant"));
export const breakGlass = (projectId: string, id: string, reason: string): Promise<WriteResult> =>
  obsWrite(`${item(id)}/break-glass`, { project_id: projectId, reason }, press(id, "Read (audited)"));

/** BOSSAPP-878: hold a work item's whole tree (dry run first), and restore it exactly. */
export const holdTree = (projectId: string, id: string, dryRun: boolean, mode: "pause" | "drain" = "pause", reason = ""): Promise<WriteResult> =>
  obsWrite("/v1/work-holds", { project_id: projectId, root_kind: "work_item", root_id: id, mode, reason, dry_run: dryRun },
    press(id, dryRun ? "Preview pause" : "Pause all"));
export const restoreHold = (projectId: string, holdId: string, dryRun: boolean): Promise<WriteResult> =>
  obsWrite(`/v1/work-holds/${encodeURIComponent(holdId)}/restore`, { project_id: projectId, dry_run: dryRun },
    press(holdId, dryRun ? "Preview restore" : "Restore"));
