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

export type ItemRef = { id: string; human_key: string | null; title: string; status: WorkStatus };
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
  // detail only
  relations?: { kind: string; item: ItemRef }[];
  runs?: { kind: string; id: string; status: string }[];
  stages?: StageEvent[];
};
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
  return { id: str(o.id), human_key: str(o.human_key) || null, title: str(o.title), status: status(o.status) };
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
