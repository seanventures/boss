"use client";

/**
 * Archive and restore a project (BOSSAPP-768 S5; KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §2.7, §5.9).
 *
 * Same-origin `/v1/…` through the browser proxy, so the browser never holds a credential. Writes
 * go through `apiWrite` (one press, one effect; a stale second factor opens "Confirm it's you"
 * and the press is sent once more). Each refusal answers in the API's own sentence: a 409
 * `archive_refused` says why this project cannot be archived (the home project, the org's last
 * open one). The API decides `project:delete`; the console only mirrors it.
 */
import { apiWrite } from "../lib/api-write";
import { declareAction } from "../lib/identity/guarded-action";
import { notifyChanged } from "./changed";

export type ArchiveState = {
  id: string;
  name: string;
  archived: boolean;
  archived_at: string | null;
  archived_by: string | null;
  /** While open: what archiving would pause, counted, and the sentences the dialog shows. */
  counts?: Record<string, number>;
  stops?: string[];
  keeps?: string[];
  changed?: boolean;
};

export type ArchivedProject = { id: string; name: string; archived_at: string; archived_by: string | null };

export type Result<T> = { ok: true; data: T } | { ok: false; error: string; status?: number };

const enc = encodeURIComponent;

function sentence(body: unknown, status: number): string {
  const b = (body ?? {}) as { detail?: unknown };
  const d = b.detail;
  if (typeof d === "string" && d) return d;
  if (d && typeof d === "object") {
    const m = (d as { message?: unknown }).message;
    if (typeof m === "string" && m) return m;
  }
  return `The API answered ${status}.`;
}

async function answer<T>(r: Response): Promise<Result<T>> {
  const data = await r.json().catch(() => null);
  if (!r.ok) return { ok: false, error: sentence(data, r.status), status: r.status };
  return { ok: true, data: (data ?? {}) as T };
}

async function get<T>(path: string): Promise<Result<T>> {
  try {
    return await answer<T>(await fetch(path, { cache: "no-store" }));
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "The API did not answer." };
  }
}

async function post<T>(path: string, body: Record<string, unknown>, label: string): Promise<Result<T>> {
  try {
    const r = await apiWrite(path, {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }, { press: { scope: "project-archive", label } });
    return await answer<T>(r);
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "The API did not answer." };
  }
}

/** The project's archive state and, while open, what archiving it pauses. */
export const readArchive = (projectId: string) =>
  get<ArchiveState>(`/v1/projects/${enc(projectId)}/archive`);

/** The org's archived projects the caller may read (GET /v1/projects?include_archived=true). */
export async function listArchivedProjects(orgId?: string): Promise<Result<ArchivedProject[]>> {
  const got = await get<{ items?: Array<ArchivedProject & { org_id?: string; archived_at?: string | null }> }>(
    "/v1/projects?include_archived=true");
  if (!got.ok) return got;
  const items = (got.data.items ?? [])
    .filter((p) => !!p.archived_at && (!orgId || !p.org_id || p.org_id === orgId))
    .map((p) => ({ id: p.id, name: p.name, archived_at: String(p.archived_at), archived_by: p.archived_by ?? null }));
  return { ok: true, data: items };
}

/** Archive: hidden from the picker, no new work, every scheduler skips it. Nothing is deleted. */
export async function archiveProject(projectId: string, note?: string): Promise<Result<ArchiveState>> {
  const done = await post<ArchiveState>(`/v1/projects/${enc(projectId)}/archive`,
    { step_up: "confirmed", ...(note?.trim() ? { note: note.trim() } : {}) }, "Archive project");
  if (done.ok) notifyChanged("projects");
  return done;
}

/** Restore: listed again, takes work again, its schedules and monitors resume. */
export async function restoreProject(projectId: string): Promise<Result<ArchiveState>> {
  const done = await post<ArchiveState>(`/v1/projects/${enc(projectId)}/unarchive`,
    { step_up: "confirmed" }, "Restore project");
  if (done.ok) notifyChanged("projects");
  return done;
}

// THE PERMISSION EACH ACTION DECLARES (IDENTITY.md §6.5, R4; lib/identity/guarded-action.ts).
// A declaration for the console to mirror — disabled with the reason when not held — never a
// decision: the API checks every call. tests/identity-mirroring.test.ts keeps this table whole.
declareAction(readArchive, "project:read");
declareAction(listArchivedProjects, "project:read");
declareAction(archiveProject, "project:delete");
declareAction(restoreProject, "project:delete");
