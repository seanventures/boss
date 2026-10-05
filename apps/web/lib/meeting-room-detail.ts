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
