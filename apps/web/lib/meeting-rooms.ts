import { BossApiError, type Meeting, type MeetingRole } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type { Meeting, MeetingRole } from "@boss/api-client";

export type MeetingRoomsFeed = {
  apiReachable: boolean;
  blockedReason?: string;
  items: Meeting[];
  roles: MeetingRole[];
  totals?: { panels: number; prds: number } | null;
};

export async function fetchMeetingRooms(projectId = PROJECT_ID, active = true): Promise<MeetingRoomsFeed> {
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return { apiReachable: false, blockedReason: "not requested", items: [], roles: [] };
  try {
    const [data, rolesData] = await Promise.all([
      client.getMeetings(resolveProjectId(projectId)),
      client.getMeetingRoles(resolveProjectId(projectId)).catch(() => ({ roles: [] as MeetingRole[], default_role_keys: [] })),
    ]);
    return { apiReachable: true, items: data.items, roles: rolesData.roles ?? [], totals: data.totals ?? null };
  } catch (error) {
    const reason =
      error instanceof BossApiError
        ? `API returned ${error.status}`
        : error instanceof Error
          ? error.message
          : "Unknown API error";
    return { apiReachable: false, blockedReason: reason, items: [], roles: [] };
  }
}
