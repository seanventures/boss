import {
  BossApiError,
  type SelfImprovementProposalSummary,
} from "@boss/api-client";

import { client, PROJECT_ID } from "./client";

export type SelfImprovementFeedItem = {
  id: string;
  taskId: string;
  title: string;
  summary: string;
  changeRef: string;
  diffSummary: string;
  status: string;
  riskTier: number;
  statusNote?: string;
  currentDecisionId?: string;
  proposalArtifactId?: string;
  canaryArtifactId?: string;
  rolloutArtifactId?: string;
  rollbackArtifactId?: string;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
};

export type SelfImprovementFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: SelfImprovementFeedItem[];
};

function mapProposal(
  payload: SelfImprovementProposalSummary,
): SelfImprovementFeedItem {
  return {
    id: payload.id,
    taskId: payload.task_id,
    title: payload.title,
    summary: payload.summary,
    changeRef: payload.change_ref,
    diffSummary: payload.diff_summary,
    status: payload.status,
    riskTier: payload.risk_tier,
    statusNote: payload.status_note,
    currentDecisionId: payload.current_decision_id,
    proposalArtifactId: payload.proposal_artifact_id,
    canaryArtifactId: payload.canary_artifact_id,
    rolloutArtifactId: payload.rollout_artifact_id,
    rollbackArtifactId: payload.rollback_artifact_id,
    createdBy: payload.created_by,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
  };
}

export function emptySelfImprovementFeed(reason?: string): SelfImprovementFeed {
  return {
    projectId: PROJECT_ID,
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchSelfImprovementFeed(): Promise<SelfImprovementFeed> {
  try {
    const payload = await client.listSelfImprovementProposals(PROJECT_ID);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapProposal),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptySelfImprovementFeed(`API returned ${error.status}`);
    }
    return emptySelfImprovementFeed(
      error instanceof Error ? error.message : "Unknown API error",
    );
  }
}
