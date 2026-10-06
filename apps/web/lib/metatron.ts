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

export function unavailableMetatron(
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

export async function fetchMetatronCaptainSession(
  projectId = PROJECT_ID,
  actor = "executive_console",
): Promise<{
  thread_id?: string;
  messages?: Array<{ id?: string; role?: string; content?: string }>;
  memories?: Array<{ id?: string; content?: string }>;
  goals?: Array<Record<string, unknown>>;
  watch?: { findings?: Array<Record<string, unknown>> } | null;
  unavailable?: string;
}> {
  const pid = resolveProjectId(projectId);
  try {
    const { API_URL, authHeaders } = await import("./client");
    const { apiWrite } = await import("./api-write");
    const { loadPath } = await import("./writing-get");
    // A writing GET (it opens the session): its POST twin, through the write path.
    const response = await apiWrite(
      loadPath(`${API_URL}/v1/metatron/session?project_id=${encodeURIComponent(pid)}&actor=${encodeURIComponent(actor)}`),
      { method: "POST", headers: authHeaders(), cache: "no-store" },
    );
    if (!response.ok) {
      return {
        unavailable: `Metatron captain session not mounted (${response.status}).`,
        messages: [],
        memories: [],
        goals: [],
      };
    }
    return (await response.json()) as {
      thread_id?: string;
      messages?: Array<{ id?: string; role?: string; content?: string }>;
      memories?: Array<{ id?: string; content?: string }>;
      goals?: Array<Record<string, unknown>>;
      watch?: { findings?: Array<Record<string, unknown>> } | null;
    };
  } catch {
    return {
      unavailable: "Metatron captain could not reach the control plane.",
      messages: [],
      memories: [],
      goals: [],
    };
  }
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
      return unavailableMetatron(
        question.trim(),
        `API returned ${error.status}`,
        projectId,
      );
    }
    return unavailableMetatron(
      question.trim(),
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
