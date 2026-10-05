import type { Meeting } from "@boss/api-client";

/**
 * One meeting room, by id, through the console's same-origin proxy (app/v1/[...path]) — what a
 * workflow's panel step and a deep link open. The list the page carries is the newest 25, so a
 * panel held before those, or one that started a second ago, is read here instead.
 */
export type MeetingRoomResult = { ok: true; meeting: Meeting } | { ok: false; error: string };

export async function fetchMeetingRoom(
  meetingId: string,
  projectId: string,
  fetcher: typeof fetch = fetch,
): Promise<MeetingRoomResult> {
  try {
    const r = await fetcher(
      `/v1/meeting-rooms/${encodeURIComponent(meetingId)}?project_id=${encodeURIComponent(projectId)}`,
      { cache: "no-store" },
    );
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const detail = (body as { detail?: unknown }).detail;
      return { ok: false, error: typeof detail === "string" ? detail : `HTTP ${r.status}` };
    }
    return { ok: true, meeting: body as Meeting };
  } catch {
    return { ok: false, error: "the control plane did not answer" };
  }
}

/** The panel held for a task (a dossier's "Convene a panel" links by task), or not-found. */
export async function fetchMeetingForTask(
  taskId: string,
  projectId: string,
  fetcher: typeof fetch = fetch,
): Promise<MeetingRoomResult> {
  try {
    const r = await fetcher(
      `/v1/meeting-rooms?project_id=${encodeURIComponent(projectId)}&task_id=${encodeURIComponent(taskId)}&limit=1`,
      { cache: "no-store" },
    );
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, error: `HTTP ${r.status}` };
    const first = ((body as { items?: Meeting[] }).items ?? [])[0];
    return first ? { ok: true, meeting: first } : { ok: false, error: "meeting not found" };
  } catch {
    return { ok: false, error: "the control plane did not answer" };
  }
}

/** A page of panels past the first (Panels' "Show more"). */
export async function fetchMeetingPage(
  projectId: string, offset: number, limit = 25, fetcher: typeof fetch = fetch,
): Promise<Meeting[] | null> {
  try {
    const r = await fetcher(
      `/v1/meeting-rooms?project_id=${encodeURIComponent(projectId)}&limit=${limit}&offset=${offset}`,
      { cache: "no-store" },
    );
    if (!r.ok) return null;
    return ((await r.json()) as { items?: Meeting[] }).items ?? [];
  } catch {
    return null;
  }
}

/** Words for a failed read, never a status code or an exception name. */
export function sayLoadError(error: string | null | undefined, what: "panel" | "dossier"): string {
  const e = String(error ?? "");
  if (/not found/i.test(e)) return "It may have been removed, or it belongs to another project.";
  if (/did not answer|HTTP 5\d\d|unreachable/i.test(e)) return "BOSS's control plane is not answering right now. Try again in a moment.";
  return `The ${what} did not open. Try again in a moment.`;
}
