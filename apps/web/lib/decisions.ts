import {
  BossApiError,
  type DecisionCard as ApiDecisionCard,
} from "@boss/api-client";
import { client, PROJECT_ID } from "./client";

export type DecisionFeedItem = {
  id: string;
  taskId?: string;
  question: string;
  status: string;
  riskTier: number;
  defaultAction?: string;
  recommendation: Record<string, unknown>;
  options: Array<Record<string, unknown>>;
  costSummary: Record<string, unknown>;
  deadlineAt?: string;
  approvedBy?: string;
  approvedAt?: string;
  delegatedTo?: string;
  delegatedBy?: string;
  delegatedAt?: string;
  revisionRequestedBy?: string;
  revisionRequestedAt?: string;
  executedBy?: string;
  executedAt?: string;
  resolutionNote?: string;
  externalRef?: string;
  createdAt: string;
  updatedAt: string;
};

export type DecisionFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: DecisionFeedItem[];
};

function mapDecision(payload: ApiDecisionCard): DecisionFeedItem {
  return {
    id: payload.id,
    taskId: payload.task_id,
    question: payload.question,
    status: payload.status,
    riskTier: payload.risk_tier,
    defaultAction: payload.default_action,
    recommendation: payload.recommendation,
    options: payload.options,
    costSummary: payload.cost_summary,
    deadlineAt: payload.deadline_at,
    approvedBy: payload.approved_by,
    approvedAt: payload.approved_at,
    delegatedTo: payload.delegated_to,
    delegatedBy: payload.delegated_by,
    delegatedAt: payload.delegated_at,
    revisionRequestedBy: payload.revision_requested_by,
    revisionRequestedAt: payload.revision_requested_at,
    executedBy: payload.executed_by,
    executedAt: payload.executed_at,
    resolutionNote: payload.resolution_note,
    externalRef: payload.external_ref,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
  };
}

export function emptyDecisionFeed(reason?: string): DecisionFeed {
  return {
    projectId: PROJECT_ID,
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchDecisionFeed(): Promise<DecisionFeed> {
  try {
    const payload = await client.listDecisions(PROJECT_ID);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapDecision),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyDecisionFeed(`API returned ${error.status}`);
    }
    return emptyDecisionFeed(
      error instanceof Error ? error.message : "Unknown API error",
    );
  }
}
