import {
  BossApiError,
  type DecisionCard as ApiDecisionCard,
} from "@boss/api-client";
import { client, PROJECT_ID, resolveProjectId } from "./client";

export type DecisionFeedItem = {
  id: string;
  taskId?: string;
  question: string;
  status: string;
  riskTier: number;
  defaultAction?: string;
  reason?: string;
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

/** CEO inbox default: only cards that still need a human. */
export const DEFAULT_DECISION_INBOX_FILTER = "needs_action" as const;

export const DECISION_INBOX_FILTERS = [
  "needs_action",
  "resolved",
  "all",
] as const;

export type DecisionInboxFilter = (typeof DECISION_INBOX_FILTERS)[number];

export const ACTIONABLE_DECISION_STATUSES = ["pending", "delegated"] as const;

export const DECISION_INBOX_EMPTY_TITLE = "Queue is clear";

export const DECISION_INBOX_EMPTY_HOWTO = [
  "Budget reservations or spend that need a human gate",
  "Pull-request or merge reviews from the software factory",
  "TPM / credit / model-limit escalations that would otherwise deadletter",
  "Agents hitting a governance ceiling (plan, deploy, autonomy)",
] as const;

export function isActionableDecision(item: { status: string }): boolean {
  return item.status === "pending" || item.status === "delegated";
}

export function parseDecisionInboxFilter(
  raw?: string | null,
): DecisionInboxFilter {
  if (raw === "needs_action" || raw === "resolved" || raw === "all") {
    return raw;
  }
  if (raw === "pending" || raw === "actionable") {
    return "needs_action";
  }
  return DEFAULT_DECISION_INBOX_FILTER;
}

export function filterDecisionInbox<T extends { status: string }>(
  items: T[],
  filter: DecisionInboxFilter = DEFAULT_DECISION_INBOX_FILTER,
): T[] {
  if (filter === "all") {
    return items;
  }
  if (filter === "resolved") {
    return items.filter((item) => !isActionableDecision(item));
  }
  return items.filter(isActionableDecision);
}

export function decisionInboxHref(options: {
  orgId?: string;
  filter?: DecisionInboxFilter;
}): string {
  const params = new URLSearchParams();
  if (options.orgId) {
    params.set("org", options.orgId);
  }
  params.set("area", "decisions");
  params.set(
    "decision_filter",
    options.filter ?? DEFAULT_DECISION_INBOX_FILTER,
  );
  return `/?${params.toString()}`;
}

function mapDecision(payload: ApiDecisionCard): DecisionFeedItem {
  return {
    id: payload.id,
    taskId: payload.task_id,
    question: payload.question,
    status: payload.status,
    riskTier: payload.risk_tier,
    defaultAction: payload.default_action,
    reason: payload.reason,
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

export function emptyDecisionFeed(reason?: string, projectId = PROJECT_ID): DecisionFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchDecisionFeed(
  projectId = PROJECT_ID,
): Promise<DecisionFeed> {
  try {
    const payload = await client.listDecisions(resolveProjectId(projectId));
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapDecision),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyDecisionFeed(`API returned ${error.status}`, projectId);
    }
    return emptyDecisionFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
