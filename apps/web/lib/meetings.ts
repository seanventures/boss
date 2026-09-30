import { BossApiError, type MeetingSummary } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type MeetingFeedItem = {
  id: string;
  title: string;
  purpose: string;
  status: string;
  participants: string[];
  agenda: string[];
  transcriptArtifactId?: string;
  minutesArtifactId?: string;
  actionTaskIds: string[];
  decisions: unknown[];
  dissentRecords: unknown[];
  blockedState?: Record<string, unknown> | null;
  completedAt?: string;
  updatedAt: string;
};

export type MeetingFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: MeetingFeedItem[];
};

function mapMeeting(payload: MeetingSummary): MeetingFeedItem {
  return {
    id: payload.id,
    title: payload.title,
    purpose: payload.purpose,
    status: payload.status,
    participants: payload.participants ?? [],
    agenda: payload.agenda ?? [],
    transcriptArtifactId: payload.transcript_artifact_id,
    minutesArtifactId: payload.minutes_artifact_id,
    actionTaskIds: payload.action_task_ids ?? [],
    decisions: payload.decisions ?? [],
    dissentRecords: payload.dissent_records ?? [],
    blockedState: payload.blocked_state,
    completedAt: payload.completed_at,
    updatedAt: payload.updated_at,
  };
}

export function emptyMeetingFeed(
  reason?: string,
  projectId = PROJECT_ID,
): MeetingFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchMeetingFeed(
  projectId = PROJECT_ID,
  limit = 8,
): Promise<MeetingFeed> {
  try {
    const payload = await client.listMeetings(resolveProjectId(projectId));
    return {
      projectId: payload.project_id ?? resolveProjectId(projectId),
      apiReachable: true,
      items: payload.items.slice(0, limit).map(mapMeeting),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyMeetingFeed(`API returned ${error.status}`, projectId);
    }
    return emptyMeetingFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
