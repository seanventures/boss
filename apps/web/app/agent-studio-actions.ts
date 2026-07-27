"use server";

import { revalidatePath } from "next/cache";
import { API_KEY, API_URL } from "../lib/client";

export type StudioActionResult = {
  ok: boolean;
  message: string;
  item?: Record<string, unknown>;
};

async function post(
  path: string,
  body: Record<string, unknown>,
): Promise<StudioActionResult> {
  try {
    const response = await fetch(`${API_URL}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(API_KEY ? { Authorization: `Bearer ${API_KEY}` } : {}),
      },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    const payload = (await response.json()) as Record<string, unknown>;
    if (!response.ok) {
      return {
        ok: false,
        message:
          typeof payload.detail === "string"
            ? payload.detail
            : `Agent Studio returned ${response.status}.`,
      };
    }
    revalidatePath("/");
    return {
      ok: true,
      message: describeResult(payload),
      item: payload,
    };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : "Agent Studio request failed.",
    };
  }
}

function describeResult(payload: Record<string, unknown>): string {
  if (payload.valid === true) {
    const spec = payload.normalized_spec as Record<string, unknown> | undefined;
    const steps = Array.isArray(spec?.steps) ? spec.steps.length : 0;
    return `Draft passed graph, binding, and side-effect checks (${steps} steps).`;
  }
  const result = payload.result as Record<string, unknown> | undefined;
  if (typeof result?.summary === "string") return result.summary;
  if (typeof payload.status === "string") {
    return `Recorded ${String(payload.status).replaceAll("_", " ")} state.`;
  }
  return "Agent Studio accepted the operation.";
}

export async function testStudioAgent(
  projectId: string,
  versionId: string,
): Promise<StudioActionResult> {
  return post(`/v1/agent-studio/agent-versions/${versionId}/tests`, {
    project_id: projectId,
    actor: "vp_agents",
    scenario: {
      objective: "Compile this AgentVersion and prove its authority and safety envelope.",
    },
    assertions: [],
  });
}

export async function runStudioAgent(
  projectId: string,
  versionId: string,
  objective: string,
): Promise<StudioActionResult> {
  return post(`/v1/agent-studio/agent-versions/${versionId}/runs`, {
    project_id: projectId,
    actor: "vp_agents",
    input: { objective, evidence_refs: ["agent-studio:operator-input"] },
    dedupe_key: `studio-ui:${versionId}:${crypto.randomUUID()}`,
  });
}

export async function runStudioPanel(
  projectId: string,
  versionId: string,
  objective: string,
): Promise<StudioActionResult> {
  return post(`/v1/agent-studio/panel-versions/${versionId}/runs`, {
    project_id: projectId,
    actor: "vp_agents",
    input: { objective, evidence_refs: ["agent-studio:operator-input"] },
    context: { initiated_from: "agent_studio_ui" },
    dedupe_key: `studio-panel-ui:${versionId}:${crypto.randomUUID()}`,
  });
}

export async function validateStudioWorkflow(
  projectId: string,
  spec: Record<string, unknown>,
): Promise<StudioActionResult> {
  return post("/v1/agent-studio/workflows/validate", {
    project_id: projectId,
    actor: "ceo",
    spec,
  });
}

export async function createStudioWorkflow(
  projectId: string,
  workflowKey: string,
  name: string,
  description: string,
  spec: Record<string, unknown>,
): Promise<StudioActionResult> {
  const definition = await post("/v1/workflows", {
    project_id: projectId,
    workflow_key: workflowKey,
    name,
    description,
    actor: "ceo",
  });
  if (!definition.ok || !definition.item?.id) return definition;
  const published = await post(
    `/v1/workflows/${encodeURIComponent(String(definition.item.id))}/versions`,
    {
      project_id: projectId,
      spec,
      activate: true,
      actor: "ceo",
    },
  );
  if (!published.ok) {
    return {
      ...published,
      message: `Workflow identity was created, but its first version was not published: ${published.message}`,
    };
  }
  return {
    ...published,
    message: `Created ${name} and published its first immutable version.`,
  };
}

export async function publishStudioWorkflowVersion(
  projectId: string,
  definitionId: string,
  spec: Record<string, unknown>,
): Promise<StudioActionResult> {
  return post(`/v1/workflows/${encodeURIComponent(definitionId)}/versions`, {
    project_id: projectId,
    spec,
    activate: true,
    actor: "ceo",
  });
}

export async function runStudioWorkflow(
  projectId: string,
  versionId: string,
  objective: string,
): Promise<StudioActionResult> {
  return post("/v1/workflow-runs", {
    project_id: projectId,
    workflow_version_id: versionId,
    input: { objective, evidence_refs: ["agent-studio:operator-input"] },
    policy_snapshot: { initiated_from: "agent_studio_ui" },
    dedupe_key: `studio-workflow-ui:${versionId}:${crypto.randomUUID()}`,
    actor: "ceo",
  });
}
