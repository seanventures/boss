import {
  BossApiError,
  type DecisionCard as ApiDecisionCard,
} from "@boss/api-client";
import { client, PROJECT_ID, resolveProjectId } from "./client";
import { parsePrdRecord, type PrdRecord } from "./prd-record";

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
  /** Who approved it, by name (the server resolves it): the card never shows an actor id. */
  approvedByName?: string | null;
  /** Answered by the approver's approval delegate (IDENTITY.md §6.15): the delegate's name, and
   *  the grant. approvedBy stays the person the delegate answered for. */
  viaDelegate?: string | null;
  viaDelegateName?: string | null;
  viaDelegation?: string | null;
  viaForName?: string | null;
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
  /** What a person can do with it, decided by the engine (services/decision_actions.py).
   *  Absent from an older API, in which case the inbox falls back to approve/reject. */
  kind?: DecisionKind;
  allowedActions?: DecisionActionOffer[];
  guidance?: DecisionGuidance | null;
  /** Where the card belongs, decided by the engine (decision_actions.attention) with the same
   *  rule the Command Center counts by. Absent from an older API: see decisionAttention. */
  attention?: DecisionAttention;
  /** A meeting's PRD (engine prd_record.links, BOSSAPP-929): the document, the panel that argued
   *  it, and its file in the repository. Absent for any other decision. */
  prd?: PrdRecord | null;
};

export type DecisionAttention = "needs_action" | "expired" | "resolved";

export type DecisionKind =
  | "refusal" | "deploy_gate" | "workflow_gate" | "architecture_review" | "pull_request"
  | "general";

export type DecisionActionOffer = {
  action: "approve" | "reject" | "revise" | "delegate" | "execute" | "settle";
  /** Which settlement a legacy-exposure card records. Both buttons share action "settle". */
  settle_at?: "zero" | "estimate" | null;
  label: string;
  tone: "approve" | "reject" | "neutral";
  effect: string;
  /** A field the action cannot go without. */
  needs?: "note" | "delegate_to" | null;
};

export type DecisionGuidance = {
  what_happened: string;
  what_to_do: string;
  /** The console area where the cause is fixed. */
  area: string;
  link_label: string;
};

/** The actions to draw. The engine's list when it sent one — older APIs did not. */
export function offeredActions(item: DecisionFeedItem): DecisionActionOffer[] {
  if (item.allowedActions) return item.allowedActions;
  if (item.status === "approved") {
    return [{ action: "execute", label: "Mark executed", tone: "approve",
              effect: "Records what was carried out.", needs: "note" }];
  }
  if (!isAnswerableDecision(item)) return [];
  return [
    { action: "approve", label: "Approve", tone: "approve", effect: "", needs: null },
    { action: "reject", label: "Reject", tone: "reject", effect: "", needs: null },
  ];
}

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
  "expired",
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

type AttentionInput = {
  status: string;
  attention?: DecisionAttention;
  allowedActions?: DecisionActionOffer[];
};

/** Where a card belongs: the engine's answer, the same one the Command Center counts by.
 *
 *  Review round 2: this used to be "any card the engine offers an answer on", which put every
 *  expired hold-default decision under the default Needs action view — 127 of 165 on the live
 *  project, some offering "Approve and merge" on pull requests from weeks ago — while the
 *  Command Center counted pending and delegated only. The two disagreed about what was waiting.
 *  The fallback is for an older API that sends no `attention`. */
export function decisionAttention(item: AttentionInput): DecisionAttention {
  if (item.attention) return item.attention;
  if (item.status === "pending" || item.status === "delegated") return "needs_action";
  const answerable = (item.allowedActions ?? []).some(
    (offer) => offer.action === "approve" || offer.action === "reject");
  if (!answerable || item.status === "approved") return "resolved";
  return item.status === "expired" ? "expired" : "needs_action";
}

/** Waiting on the operator now: the default Needs action view, and the Command Center count. */
export function isActionableDecision(item: AttentionInput): boolean {
  return decisionAttention(item) === "needs_action";
}

/** Can still be answered, including an expired hold decision that applied nothing. The card
 *  draws its buttons for these. */
export function isAnswerableDecision(item: AttentionInput): boolean {
  const where = decisionAttention(item);
  return where === "needs_action" || where === "expired";
}

export function parseDecisionInboxFilter(
  raw?: string | null,
): DecisionInboxFilter {
  if (raw === "needs_action" || raw === "expired" || raw === "resolved" || raw === "all") {
    return raw;
  }
  if (raw === "pending" || raw === "actionable") {
    return "needs_action";
  }
  return DEFAULT_DECISION_INBOX_FILTER;
}

export function filterDecisionInbox<T extends AttentionInput>(
  items: T[],
  filter: DecisionInboxFilter = DEFAULT_DECISION_INBOX_FILTER,
): T[] {
  if (filter === "all") {
    return items;
  }
  return items.filter((item) => decisionAttention(item) === filter);
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
    approvedByName: (payload as { approved_by_name?: string | null }).approved_by_name ?? null,
    viaDelegate: (payload as { via_delegate?: string | null }).via_delegate ?? null,
    viaDelegateName: (payload as { via_delegate_name?: string | null }).via_delegate_name ?? null,
    viaDelegation: (payload as { via_delegation?: string | null }).via_delegation ?? null,
    viaForName: (payload as { via_for_name?: string | null }).via_for_name ?? null,
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
    ...contractOf(payload),
    prd: parsePrdRecord((payload as { prd?: unknown }).prd),
  };
}

/** The engine's action contract, read defensively: the generated client type predates it. */
function contractOf(payload: ApiDecisionCard): Pick<
  DecisionFeedItem, "kind" | "allowedActions" | "guidance" | "attention"> {
  const extra = payload as ApiDecisionCard & {
    kind?: DecisionKind;
    allowed_actions?: DecisionActionOffer[];
    guidance?: DecisionGuidance | null;
    attention?: DecisionAttention | null;
  };
  return {
    ...(extra.attention ? { attention: extra.attention } : {}),
    ...(extra.kind ? { kind: extra.kind } : {}),
    ...(Array.isArray(extra.allowed_actions) ? { allowedActions: extra.allowed_actions } : {}),
    ...(extra.guidance !== undefined ? { guidance: extra.guidance } : {}),
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
