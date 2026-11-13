/**
 * THE PROJECT WIKI'S CALLS (BOSSAPP-891): one door for every read and write the Wiki tab makes.
 *
 * Reads go through kgRead (the same-origin /v1 proxy, failures said in words). Writes go through
 * lib/api-write.ts (one press, one effect) and are registered with the permission their route
 * checks in lib/identity/write-permissions.ts. A person's Save is written at once; an agent's
 * change arrives as a pending change (a decision in Approvals) and is never written from here.
 */
import { apiErrorText } from "./api-error";
import { apiWrite, type WriteMethod } from "./api-write";
import { API_URL } from "./client";
import type { Permission } from "./identity/permissions.gen";
import { kgRead, type Read } from "./kg-view";

export const WIKI_MANAGE: Permission = "knowledge:manage";

type Who = { projectId: string; actor: string };

export type WikiPageRow = {
  slug: string; title: string; revision_no: number; author_kind: "person" | "agent" | "system";
  author_id: string; updated_at: string | null; maintained_by: string | null; pending: number; chars: number;
};
export type WikiRevision = {
  id: string; revision_no: number; title: string; body: string; author_kind: string; author_id: string;
  source_note: string; created_at: string | null; restored_from: string | null; current?: boolean;
};
export type WikiPending = { id: string; decision_id: string; requested_by: string; diff: string; created_at: string | null };
export type WikiPage = {
  slug: string; title: string; body: string; maintained_by: string | null; revision: WikiRevision;
  links: string[]; backlinks: { slug: string; title: string }[]; pending: WikiPending[];
};
export type Wrote<T> = { ok: true; data: T } | { ok: false; status: number; error: string };

const q = encodeURIComponent;

export function getWikiPages(w: Who, signal?: AbortSignal): Promise<Read<{ pages: WikiPageRow[] }>> {
  return kgRead("/v1/wiki/pages", { project_id: w.projectId }, signal);
}

export function getWikiPage(w: Who, slug: string, signal?: AbortSignal): Promise<Read<WikiPage>> {
  return kgRead(`/v1/wiki/pages/${q(slug)}`, { project_id: w.projectId }, signal);
}

export function getWikiRevisions(w: Who, slug: string, signal?: AbortSignal): Promise<Read<{ revisions: WikiRevision[] }>> {
  return kgRead(`/v1/wiki/pages/${q(slug)}/revisions`, { project_id: w.projectId }, signal);
}

async function write<T>(method: WriteMethod, path: string, body: Record<string, unknown>,
                        press: { scope: string; label: string }): Promise<Wrote<T>> {
  try {
    const r = await apiWrite(`${API_URL}${path}`, {
      method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), cache: "no-store",
    }, { press });
    let parsed: unknown = null;
    try { parsed = await r.json(); } catch { parsed = null; }
    if (!r.ok) return { ok: false, status: r.status, error: apiErrorText(parsed, r.status) };
    return { ok: true, data: parsed as T };
  } catch (e) {
    return { ok: false, status: 0, error: e instanceof Error ? e.message : String(e) };
  }
}

export function saveWikiPage(w: Who, slug: string, title: string, body: string) {
  return write<{ changed: boolean; revision: WikiRevision }>("PUT", `/v1/wiki/pages/${q(slug)}`,
    { project_id: w.projectId, actor: w.actor, title, body }, { scope: `wiki:${slug}`, label: "Save" });
}

export function restoreWikiPage(w: Who, slug: string, revision: number) {
  return write<{ revision: WikiRevision }>("POST", `/v1/wiki/pages/${q(slug)}/restore`,
    { project_id: w.projectId, actor: w.actor, revision }, { scope: `wiki:${slug}`, label: "Restore" });
}

export function maintainWiki(w: Who) {
  return write<{ written: string[]; proposed: string[]; unchanged: string[] }>("POST", "/v1/wiki/maintain",
    { project_id: w.projectId, actor: w.actor }, { scope: `wiki-maintain:${w.projectId}`, label: "Refresh from the graph" });
}

/** A title as a slug, the way the API checks it (lower-case letters, digits and dashes). */
export function slugOf(title: string): string {
  return title.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80);
}
