import {
  BossApiError,
  type CommsChannelPolicySummary,
  type InboxMessageSummary,
  type OutboxMessageSummary,
} from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type OutboxFeedItem = {
  id: string;
  channel: string;
  subject?: string;
  body: string;
  status: string;
  receiptId?: string;
  createdAt: string;
};

export type CommsPolicyItem = {
  id: string;
  provider: string;
  deliveryMode: string;
  allowedDataBoundaries: string[];
  rolloutNotes?: string;
  status: string;
  createdBy: string;
  updatedAt: string;
};

export type OutboxFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: OutboxFeedItem[];
  policies: CommsPolicyItem[];
};

export type InboxFeedItem = {
  id: string;
  channel: string;
  subject?: string;
  status: string;
  rawRef?: string;
  createdAt: string;
};

export type InboxFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: InboxFeedItem[];
};

function mapOutbox(payload: OutboxMessageSummary): OutboxFeedItem {
  return {
    id: payload.id,
    channel: payload.channel,
    subject: payload.subject,
    body: payload.body,
    status: payload.status,
    receiptId: payload.receipt_id,
    createdAt: payload.created_at,
  };
}

function mapPolicy(payload: CommsChannelPolicySummary): CommsPolicyItem {
  return {
    id: payload.id,
    provider: payload.provider,
    deliveryMode: payload.delivery_mode,
    allowedDataBoundaries: payload.allowed_data_boundaries,
    rolloutNotes: payload.rollout_notes,
    status: payload.status,
    createdBy: payload.created_by,
    updatedAt: payload.updated_at,
  };
}

function mapInbox(payload: InboxMessageSummary): InboxFeedItem {
  return {
    id: payload.id,
    channel: payload.channel,
    subject: payload.subject,
    status: payload.status,
    rawRef: payload.raw_ref,
    createdAt: payload.created_at,
  };
}

export function emptyOutboxFeed(reason?: string, projectId = PROJECT_ID): OutboxFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
    policies: [],
  };
}

export function emptyInboxFeed(reason?: string, projectId = PROJECT_ID): InboxFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchOutboxFeed(
  projectId = PROJECT_ID,
  limit = 8,
  active = true,
): Promise<OutboxFeed> {
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return emptyOutboxFeed("not requested", projectId);
  const resolvedProjectId = resolveProjectId(projectId);
  try {
    const [payload, policies] = await Promise.all([
      client.listOutboxMessages(resolvedProjectId, limit),
      client.listCommsChannelPolicies(resolvedProjectId),
    ]);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapOutbox),
      policies: policies.items.map(mapPolicy),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyOutboxFeed(`API returned ${error.status}`, projectId);
    }
    return emptyOutboxFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}

export async function fetchInboxFeed(
  projectId = PROJECT_ID,
  limit = 8,
): Promise<InboxFeed> {
  try {
    const payload = await client.listInboxMessages(resolveProjectId(projectId), limit);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapInbox),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyInboxFeed(`API returned ${error.status}`, projectId);
    }
    return emptyInboxFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
