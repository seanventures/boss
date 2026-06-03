import {
  BossApiError,
  type InboxMessageSummary,
  type OutboxMessageSummary,
} from "@boss/api-client";

import { client, PROJECT_ID } from "./client";

export type OutboxFeedItem = {
  id: string;
  channel: string;
  subject?: string;
  body: string;
  status: string;
  receiptId?: string;
  createdAt: string;
};

export type OutboxFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: OutboxFeedItem[];
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

export function emptyOutboxFeed(reason?: string): OutboxFeed {
  return {
    projectId: PROJECT_ID,
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export function emptyInboxFeed(reason?: string): InboxFeed {
  return {
    projectId: PROJECT_ID,
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchOutboxFeed(limit = 8): Promise<OutboxFeed> {
  try {
    const payload = await client.listOutboxMessages(PROJECT_ID, limit);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapOutbox),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyOutboxFeed(`API returned ${error.status}`);
    }
    return emptyOutboxFeed(
      error instanceof Error ? error.message : "Unknown API error",
    );
  }
}

export async function fetchInboxFeed(limit = 8): Promise<InboxFeed> {
  try {
    const payload = await client.listInboxMessages(PROJECT_ID, limit);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapInbox),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyInboxFeed(`API returned ${error.status}`);
    }
    return emptyInboxFeed(
      error instanceof Error ? error.message : "Unknown API error",
    );
  }
}
