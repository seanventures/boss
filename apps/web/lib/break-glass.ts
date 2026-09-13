import { BossApiError, type BreakGlassGrantSummary } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type BreakGlassGrantFeedItem = {
  id: string;
  projectId: string;
  targetMemberId: string;
  requestedBy: string;
  reason: string;
  scope: Record<string, unknown>;
  grantedPermissions: string[];
  status: string;
  expiresAt: string;
  postmortemTaskId?: string;
  revokedBy?: string;
  revokedReason?: string;
  revokedAt?: string;
  createdAt: string;
  updatedAt: string;
};

export type BreakGlassGrantFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: BreakGlassGrantFeedItem[];
};

function mapGrant(payload: BreakGlassGrantSummary): BreakGlassGrantFeedItem {
  return {
    id: payload.id,
    projectId: payload.project_id,
    targetMemberId: payload.target_member_id,
    requestedBy: payload.requested_by,
    reason: payload.reason,
    scope: payload.scope,
    grantedPermissions: payload.granted_permissions,
    status: payload.status,
    expiresAt: payload.expires_at,
    postmortemTaskId: payload.postmortem_task_id,
    revokedBy: payload.revoked_by,
    revokedReason: payload.revoked_reason,
    revokedAt: payload.revoked_at,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
  };
}

export function emptyBreakGlassGrantFeed(
  reason?: string,
  projectId = PROJECT_ID,
): BreakGlassGrantFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchBreakGlassGrantFeed(
  projectId = PROJECT_ID,
  active = true,
): Promise<BreakGlassGrantFeed> {
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return emptyBreakGlassGrantFeed("not requested", projectId);
  try {
    const payload = await client.listBreakGlassGrants(
      resolveProjectId(projectId),
      true,
    );
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapGrant),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyBreakGlassGrantFeed(`API returned ${error.status}`, projectId);
    }
    return emptyBreakGlassGrantFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
