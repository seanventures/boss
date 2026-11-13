/**
 * DOCUMENTS ON A WORK ITEM (BOSSAPP-874; API routers/documents.py): PRDs, plans, minutes and
 * architectures with append-only revisions, a save that names its base (a stale base answers 409
 * with the revision that won), and comment threads anchored to a passage. Parsing is defensive
 * and pure (tested on its own); every write goes through obsWrite → apiWrite.
 */
import { aaRead, type ReadResult } from "./ambient";
import { obsWrite, type WriteResult } from "../app/observability/obs-write";

export type AnchorState = "exact" | "moved" | "fuzzy" | "orphaned";
export type DocComment = { id: string; author: string; body: string; created_at: string | null };
export type DocThread = {
  id: string; revision_id: string; quote: string; start: number | null; end: number | null;
  anchor_state: AnchorState; status: "open" | "resolved"; created_by: string; comments: DocComment[];
};
export type DocRevision = {
  id: string; revision_no: number; base_revision_id: string | null; author_kind: string;
  author_id: string; note: string; restored_from: string | null; created_at: string | null; body?: string;
};
export type Doc = {
  id: string; work_item_id: string | null; kind: string; title: string;
  current_revision_id: string | null; current_revision_no: number; locked_by: string | null;
  open_threads: number; current?: DocRevision | null; revisions?: DocRevision[]; threads?: DocThread[];
};
export type DocDiff = { from: number; to: number; lines: string[]; added: number; removed: number };

const obj = (v: unknown): Record<string, unknown> =>
  (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown) => (typeof v === "string" ? v : "");
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const STATES: AnchorState[] = ["exact", "moved", "fuzzy", "orphaned"];

export const ANCHOR_WORDS: Record<AnchorState, string> = {
  exact: "On its passage", moved: "Passage moved", fuzzy: "Passage reworded", orphaned: "Passage removed",
};

export function parseRevision(w: unknown): DocRevision {
  const o = obj(w);
  return {
    id: str(o.id), revision_no: Number(o.revision_no) || 0, base_revision_id: str(o.base_revision_id) || null,
    author_kind: str(o.author_kind) || "person", author_id: str(o.author_id), note: str(o.note),
    restored_from: str(o.restored_from) || null, created_at: str(o.created_at) || null,
    ...(typeof o.body === "string" ? { body: o.body } : {}),
  };
}

export function parseThread(w: unknown): DocThread {
  const o = obj(w);
  return {
    id: str(o.id), revision_id: str(o.revision_id), quote: str(o.quote), start: num(o.start), end: num(o.end),
    anchor_state: STATES.includes(o.anchor_state as AnchorState) ? o.anchor_state as AnchorState : "exact",
    status: o.status === "resolved" ? "resolved" : "open", created_by: str(o.created_by),
    comments: arr(o.comments).map(obj).map((c) => ({ id: str(c.id), author: str(c.author), body: str(c.body), created_at: str(c.created_at) || null })),
  };
}

export function parseDoc(w: unknown): Doc {
  const o = obj(w);
  return {
    id: str(o.id), work_item_id: str(o.work_item_id) || null, kind: str(o.kind) || "note",
    title: str(o.title) || "(untitled)", current_revision_id: str(o.current_revision_id) || null,
    current_revision_no: Number(o.current_revision_no) || 0, locked_by: str(o.locked_by) || null,
    open_threads: Number(o.open_threads) || 0,
    ...(o.current !== undefined ? { current: o.current ? parseRevision(o.current) : null } : {}),
    ...(Array.isArray(o.revisions) ? { revisions: arr(o.revisions).map(parseRevision) } : {}),
    ...(Array.isArray(o.threads) ? { threads: arr(o.threads).map(parseThread) } : {}),
  };
}

export function parseDiff(w: unknown): DocDiff {
  const o = obj(w);
  return { from: Number(o.from) || 0, to: Number(o.to) || 0, lines: arr(o.lines).map(str),
    added: Number(o.added) || 0, removed: Number(o.removed) || 0 };
}

/** A 409's revision that won (a stale base or someone's lock), from the refusal body. */
export function conflictOf(refusal: unknown): { revision: DocRevision | null; locked: boolean } {
  const d = obj(obj(refusal).detail);
  const current = obj(d.current);
  return { revision: Object.keys(current).length ? parseRevision(current) : null, locked: d.locked === true };
}

const q = (projectId: string) => `project_id=${encodeURIComponent(projectId)}`;
const doc = (id: string) => `/v1/documents/${encodeURIComponent(id)}`;
const press = (scope: string, label: string) => ({ scope: `doc:${scope}`, label });

export const readDocs = (projectId: string, workItemId: string): Promise<ReadResult<Doc[]>> =>
  aaRead(`/v1/documents?${q(projectId)}&work_item_id=${encodeURIComponent(workItemId)}`,
    (w) => arr(obj(w).documents).map(parseDoc));
export const readDoc = (projectId: string, id: string): Promise<ReadResult<Doc>> =>
  aaRead(`${doc(id)}?${q(projectId)}`, (w) => parseDoc(obj(w).document));
export const readDiff = (projectId: string, id: string, from: number, to: number): Promise<ReadResult<DocDiff>> =>
  aaRead(`${doc(id)}/diff?${q(projectId)}&from=${from}&to=${to}`, parseDiff);

export const createDoc = (projectId: string, workItemId: string, title: string, kind = "note"): Promise<WriteResult> =>
  obsWrite("/v1/documents", { project_id: projectId, work_item_id: workItemId, title, kind, body: "" }, press("new", "New document"));
export const saveDoc = (projectId: string, id: string, body: string, baseRevisionId: string | null): Promise<WriteResult> =>
  obsWrite(`${doc(id)}/revisions`, { project_id: projectId, body, base_revision_id: baseRevisionId }, press(id, "Save"));
export const restoreDoc = (projectId: string, id: string, revisionNo: number, baseRevisionId: string | null): Promise<WriteResult> =>
  obsWrite(`${doc(id)}/restore`, { project_id: projectId, revision_no: revisionNo, base_revision_id: baseRevisionId }, press(id, "Restore"));
export const commentOn = (projectId: string, id: string, revisionId: string, start: number, end: number, body: string): Promise<WriteResult> =>
  obsWrite(`${doc(id)}/threads`, { project_id: projectId, revision_id: revisionId, start, end, body }, press(id, "Comment"));
export const replyTo = (projectId: string, id: string, threadId: string, body: string): Promise<WriteResult> =>
  obsWrite(`${doc(id)}/threads/${encodeURIComponent(threadId)}/replies`, { project_id: projectId, body }, press(threadId, "Reply"));
export const resolveThread = (projectId: string, id: string, threadId: string, resolved: boolean): Promise<WriteResult> =>
  obsWrite(`${doc(id)}/threads/${encodeURIComponent(threadId)}/resolve`, { project_id: projectId, resolved }, press(threadId, resolved ? "Resolve" : "Reopen"));
