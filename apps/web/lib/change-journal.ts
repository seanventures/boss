import { BossApiError, type ChangeJournalItem } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type ChangeJournalFeedItem = {
  id: string;
  title: string;
  artifactType: string;
  changeKind: string;
  summary?: string;
  diffSummary?: string;
  filesChanged: string[];
  rollbackPlan?: string;
  taskId?: string;
  dataBoundary: string;
  createdAt: string;
};

export type ChangeJournalFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: ChangeJournalFeedItem[];
};

function mapChangeJournalItem(payload: ChangeJournalItem): ChangeJournalFeedItem {
  return {
    id: payload.id,
    title: payload.title,
    artifactType: payload.artifact_type,
    changeKind: payload.change_kind,
    summary: payload.summary,
    diffSummary: payload.diff_summary,
    filesChanged: payload.files_changed ?? [],
    rollbackPlan: payload.rollback_plan,
    taskId: payload.task_id,
    dataBoundary: payload.data_boundary,
    createdAt: payload.created_at,
  };
}

export function emptyChangeJournalFeed(
  reason?: string,
  projectId = PROJECT_ID,
): ChangeJournalFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchChangeJournalFeed(
  projectId = PROJECT_ID,
  limit = 8,
  active = true,
): Promise<ChangeJournalFeed> {
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return emptyChangeJournalFeed("not requested", projectId);
  try {
    const payload = await client.listChangeJournal(resolveProjectId(projectId), limit);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapChangeJournalItem),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyChangeJournalFeed(`API returned ${error.status}`, projectId);
    }
    return emptyChangeJournalFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
