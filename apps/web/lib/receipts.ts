import { BossApiError, type ReceiptResponse } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type ReceiptFeedItem = {
  id: string;
  dedupeKey: string;
  effectKind: string;
  status: string;
  providerRef?: string;
  createdByJobId?: string;
  createdAt: string;
};

export type ReceiptFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: ReceiptFeedItem[];
};

function mapReceipt(payload: ReceiptResponse): ReceiptFeedItem {
  return {
    id: payload.id,
    dedupeKey: payload.dedupe_key,
    effectKind: payload.effect_kind,
    status: payload.status,
    providerRef: payload.provider_ref,
    createdByJobId: payload.created_by_job_id,
    createdAt: payload.created_at,
  };
}

export function emptyReceiptFeed(reason?: string, projectId = PROJECT_ID): ReceiptFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchReceiptFeed(
  projectId = PROJECT_ID,
  limit = 8,
): Promise<ReceiptFeed> {
  try {
    const payload = await client.listReceipts(resolveProjectId(projectId), limit);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapReceipt),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyReceiptFeed(`API returned ${error.status}`, projectId);
    }
    return emptyReceiptFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
