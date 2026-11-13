/**
 * THE PERSONAL INBOX (BOSSAPP-875; API routers/inbox.py): the signed-in person's decisions, work
 * and failed runs, ranked by deadline, risk and how much work each blocks. Read state, snoozes
 * and archives are the person's own. Parsing is defensive and pure.
 */
import { aaRead, type ReadResult } from "./ambient";
import { obsWrite, type WriteResult } from "../app/observability/obs-write";

export type InboxKind = "decision" | "work_item" | "failed_run";
export type InboxEntry = {
  item_key: string; kind: InboxKind; ref_id: string; title: string; work_item_id: string | null;
  for_you: boolean; risk_tier: number; deadline_at: string | null; blocked_count: number;
  score: number; why: string[]; read: boolean; snoozed_until: string | null; archived: boolean;
};
export type Inbox = { items: InboxEntry[]; count: number; unread: number; snoozed: number; archived: number };

const obj = (v: unknown): Record<string, unknown> =>
  (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown) => (typeof v === "string" ? v : "");
const KINDS: InboxKind[] = ["decision", "work_item", "failed_run"];

export const KIND_WORDS: Record<InboxKind, string> = { decision: "Decide", work_item: "Work", failed_run: "Failed run" };

export function parseInbox(w: unknown): Inbox {
  const o = obj(w);
  return {
    items: arr(o.items).map(obj).map((e) => ({
      item_key: str(e.item_key), kind: KINDS.includes(e.kind as InboxKind) ? e.kind as InboxKind : "decision",
      ref_id: str(e.ref_id), title: str(e.title) || "(untitled)", work_item_id: str(e.work_item_id) || null,
      for_you: e.for_you === true, risk_tier: Number(e.risk_tier) || 0, deadline_at: str(e.deadline_at) || null,
      blocked_count: Number(e.blocked_count) || 0, score: Number(e.score) || 0, why: arr(e.why).map(str).filter(Boolean),
      read: e.read === true, snoozed_until: str(e.snoozed_until) || null, archived: e.archived === true,
    })),
    count: Number(o.count) || 0, unread: Number(o.unread) || 0, snoozed: Number(o.snoozed) || 0, archived: Number(o.archived) || 0,
  };
}

const q = (projectId: string) => `project_id=${encodeURIComponent(projectId)}`;

export const readInbox = (projectId: string, mine = true): Promise<ReadResult<Inbox>> =>
  aaRead(`/v1/inbox?${q(projectId)}&mine=${mine ? "true" : "false"}`, parseInbox);
export const markRead = (projectId: string, keys: string[], read = true): Promise<WriteResult> =>
  obsWrite("/v1/inbox/read", { project_id: projectId, item_keys: keys, read }, { scope: `inbox:${keys[0] ?? ""}`, label: read ? "Mark read" : "Mark unread" });
export const snoozeEntry = (projectId: string, key: string, minutes: number): Promise<WriteResult> =>
  obsWrite("/v1/inbox/snooze", { project_id: projectId, item_key: key, minutes }, { scope: `inbox:${key}`, label: "Snooze" });
export const archiveEntry = (projectId: string, key: string): Promise<WriteResult> =>
  obsWrite("/v1/inbox/snooze", { project_id: projectId, item_key: key, archive: true }, { scope: `inbox:${key}`, label: "Archive" });
