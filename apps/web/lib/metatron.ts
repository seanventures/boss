import {
  BossApiError,
  type MetatronQueryResponse,
} from "@boss/api-client";
import { client, PROJECT_ID, resolveProjectId } from "./client";

export type MetatronCard = {
  projectId: string;
  question: string;
  category: string;
  answer: string;
  freshnessAt?: string;
  references: Array<{
    objectType: string;
    objectId: string;
    title: string;
    status?: string;
    detail?: string;
    createdAt?: string;
  }>;
  suggestedActions: string[];
  topRisks: Array<Record<string, unknown>>;
  apiReachable: boolean;
  blockedReason?: string;
};

export function placeholderMetatron(
  question: string,
  reason: string,
  projectId = PROJECT_ID,
): MetatronCard {
  return {
    projectId: resolveProjectId(projectId),
    question,
    category: "unavailable",
    answer: "Metatron could not answer because the control-plane API is unavailable.",
    freshnessAt: undefined,
    references: [],
    suggestedActions: [],
    topRisks: [],
    apiReachable: false,
    blockedReason: reason,
  };
}

function mapMetatron(payload: MetatronQueryResponse): MetatronCard {
  return {
    projectId: payload.project_id,
    question: payload.question,
    category: payload.category,
    answer: payload.answer,
    freshnessAt: payload.freshness_at,
    references: payload.references.map((reference) => ({
      objectType: reference.object_type,
      objectId: reference.object_id,
      title: reference.title,
      status: reference.status,
      detail: reference.detail,
      createdAt: reference.created_at,
    })),
    suggestedActions: payload.suggested_actions,
    topRisks: payload.top_risks,
    apiReachable: true,
    blockedReason: undefined,
  };
}

export async function fetchMetatronQuery(
  question?: string,
  taskId?: string,
  decisionId?: string,
  projectId = PROJECT_ID,
): Promise<MetatronCard | null> {
  if (!question?.trim()) {
    return null;
  }
  try {
    return mapMetatron(
      await client.queryMetatron({
        project_id: resolveProjectId(projectId),
        question: question.trim(),
        task_id: taskId,
        decision_id: decisionId,
      }),
    );
  } catch (error) {
    if (error instanceof BossApiError) {
      return placeholderMetatron(
        question.trim(),
        `API returned ${error.status}`,
        projectId,
      );
    }
    return placeholderMetatron(
      question.trim(),
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
