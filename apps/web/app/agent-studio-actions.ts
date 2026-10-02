"use server";

import { revalidatePath } from "next/cache";
import { API_KEY, API_URL } from "../lib/client";
import type { AgentWizardSubmission } from "../lib/agent-wizard";
import { apiWrite, pressToken } from "../lib/api-write";
import { refusalFrom } from "../lib/start-refusal";

export type StudioActionResult = {
  ok: boolean;
  message: string;
  item?: Record<string, unknown>;
  /** A start refused for what the project lacks: the checklist, for "Start anyway". */
  readiness?: unknown;
};

function post(path: string, body: Record<string, unknown>): Promise<StudioActionResult> {
  return send("POST", path, body);
}

function patch(path: string, body: Record<string, unknown>): Promise<StudioActionResult> {
  return send("PATCH", path, body);
}

async function send(
  method: "POST" | "PATCH",
  path: string,
  body: Record<string, unknown>,
): Promise<StudioActionResult> {
  try {
    if (!API_URL) {
      return {
        ok: false,
        message: "BOSS API URL is not configured; cannot reach Agent Studio.",
      };
    }
    const response = await apiWrite(`${API_URL}${path}`, {
      method,
      headers: {
        "Content-Type": "application/json",
        ...(API_KEY ? { Authorization: `Bearer ${API_KEY}` } : {}),
      },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    let payload: Record<string, unknown> = {};
    try {
      payload = (await response.json()) as Record<string, unknown>;
    } catch {
      payload = {};
    }
    if (!response.ok) {
      // ONE READING OF A REFUSAL, shared with every other door that starts work. A Run refused
      // because the project lacks what the workflow needs carries the checklist as an object;
      // read here as "not a string" it became "Agent Studio returned 409." and the operator
      // never saw what was missing (review of the readiness change).
      const refusal = refusalFrom(response.status, { detail: payload.detail });
      // Undefined when the API gave no sentence, so the per-status explanations below apply.
      const detail = /^API returned \d+$/.test(refusal.error) ? undefined : refusal.error;
      if (refusal.readiness) {
        return { ok: false, message: refusal.error, readiness: refusal.readiness };
      }
      if (response.status === 403) {
        return {
          ok: false,
          message:
            detail ??
            "The signed-in member does not have role_review authority to change agents on this project.",
        };
      }
      if (response.status === 422) {
        return {
          ok: false,
          message:
            detail ??
            "Project cannot accept the starter catalog (missing roles or policy).",
        };
      }
      if (response.status === 503) {
        return {
          ok: false,
          message:
            detail ??
            "Agent Studio schema is not applied on this runtime. Apply db/schema.sql before seeding.",
        };
      }
      return {
        ok: false,
        message: detail ?? `Agent Studio returned ${response.status}.`,
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
  if (payload.status === "seeded") {
    const agents = typeof payload.agent_count === "number" ? payload.agent_count : 0;
    const panels = typeof payload.panel_count === "number" ? payload.panel_count : 0;
    return `Installed ${agents} governed starter agents and ${panels} executable panels. No work was dispatched.`;
  }
  if (payload.valid === true) {
    const spec = payload.normalized_spec as Record<string, unknown> | undefined;
    if (spec) {
      const steps = Array.isArray(spec.steps) ? spec.steps.length : 0;
      return `Draft passed graph, binding, and side-effect checks (${steps} steps).`;
    }
    return "Agent authority envelope passed validation.";
  }
  const result = payload.result as Record<string, unknown> | undefined;
  if (typeof result?.summary === "string") return result.summary;
  if (typeof payload.status === "string") {
    return `Recorded ${String(payload.status).replaceAll("_", " ")} state.`;
  }
  return "Agent Studio accepted the operation.";
}

function validationFailureMessage(payload: Record<string, unknown>): string {
  const errors = Array.isArray(payload.errors)
    ? payload.errors.filter((item): item is string => typeof item === "string")
    : [];
  return errors.length
    ? `Draft created, but validation blocked publish: ${errors.join(" ")}`
    : "Draft created, but validation blocked publish. Review the authority envelope.";
}

export async function createStudioAgent(
  submission: AgentWizardSubmission,
  publish: boolean,
): Promise<StudioActionResult> {
  const projectId = String(submission.definition.project_id);
  const actor = submission.actor;

  // THE AGENT AND ITS GRANTS FIRST, as one write. The wizard used to register a role with the
  // chosen skills written into `spec.allowed_skills` and the profile's `requested_skills` —
  // two lists the runtime never reads — so the agent it made held nothing. Now a new agent is
  // created through POST /v1/agents with its skills and MCP servers GRANTED (role_skills,
  // role_mcp_servers) in the same transaction, and an existing one is given anything it does
  // not yet hold. That refuses an existing key instead of overwriting it, so naming a new
  // agent "QA" can no longer replace the bundled qa role.
  if (submission.agent.create) {
    const made = await post("/v1/agents", submission.agent.create);
    if (!made.ok) {
      return { ...made, message: `The agent could not be created: ${made.message}` };
    }
  } else if (submission.agent.grant) {
    const granted = await patch(
      `/v1/agents/${encodeURIComponent(submission.agent.roleKey)}`, submission.agent.grant);
    if (!granted.ok) {
      return {
        ...granted,
        message: `${submission.agent.roleKey} could not be given what this profile uses: ${granted.message}`,
      };
    }
  }

  const definition = await post("/v1/agent-studio/agents", submission.definition);
  if (!definition.ok || typeof definition.item?.id !== "string") {
    return {
      ...definition,
      // THE WIZARD ADOPTS IT (lib/agent-wizard adoptCreatedAgent), so pressing Create again
      // retries the profile against this agent instead of 409-ing on "already exists".
      ...(submission.agent.create ? {
        item: {
          agent_created: submission.agent.roleKey,
          agent_name: String(submission.agent.create.name ?? submission.agent.roleKey),
        },
      } : {}),
      message: submission.agent.create
        ? `The agent ${submission.agent.roleKey} was created and holds its skills, but its Studio profile was not: ${definition.message}. Fix that and press Create again — the agent is kept and will not be made twice.`
        : definition.message,
    };
  }

  const definitionId = definition.item.id;
  const version = await post(
    `/v1/agent-studio/agents/${encodeURIComponent(definitionId)}/versions`,
    submission.version,
  );
  if (!version.ok || typeof version.item?.id !== "string") {
    return {
      ...version,
      item: { definition_id: definitionId },
      message: `Agent identity was created, but its first version was not: ${version.message}`,
    };
  }

  const versionId = version.item.id;
  const created = {
    definition_id: definitionId,
    version_id: versionId,
    role_key: submission.agent.roleKey,
    status: "draft",
  };
  if (!publish) {
    return {
      ok: true,
      message: "Agent created with its grants; its Studio profile is saved as a draft version.",
      item: created,
    };
  }

  const validation = await post(
    `/v1/agent-studio/agent-versions/${encodeURIComponent(versionId)}/validate`,
    { project_id: projectId, actor },
  );
  if (!validation.ok) {
    return {
      ...validation,
      item: created,
      message: `Draft created, but validation could not complete: ${validation.message}`,
    };
  }
  if (validation.item?.valid !== true) {
    return {
      ok: false,
      message: validationFailureMessage(validation.item ?? {}),
      item: {
        ...created,
        errors: validation.item?.errors ?? [],
        warnings: validation.item?.warnings ?? [],
      },
    };
  }

  const published = await post(
    `/v1/agent-studio/agent-versions/${encodeURIComponent(versionId)}/publish`,
    { project_id: projectId, actor },
  );
  if (!published.ok) {
    return {
      ...published,
      item: created,
      message: `Validated draft was preserved, but publish failed: ${published.message}`,
    };
  }
  return {
    ok: true,
    message: "Agent created with its grants, and its Studio profile validated and published.",
    item: {
      definition_id: definitionId,
      version_id: versionId,
      role_key: submission.agent.roleKey,
      status: "published",
      warnings: validation.item?.warnings ?? [],
    },
  };
}

export async function testStudioAgent(
  projectId: string,
  versionId: string,
  actor: string,
): Promise<StudioActionResult> {
  return post(`/v1/agent-studio/agent-versions/${versionId}/tests`, {
    project_id: projectId,
    actor,
    scenario: {
      objective: "Compile this AgentVersion and prove its authority and safety envelope.",
    },
    assertions: [],
  });
}

export async function runStudioCatalogSmoke(
  projectId: string,
  actor: string,
): Promise<StudioActionResult> {
  return post("/v1/agent-studio/catalog-smoke", {
    project_id: projectId,
    actor,
  });
}

export async function seedStudioCatalog(
  projectId: string,
  actor: string,
): Promise<StudioActionResult> {
  return post("/v1/agent-studio/catalog/seed", {
    project_id: projectId,
    actor,
  });
}

export async function runStudioAgent(
  projectId: string,
  versionId: string,
  objective: string,
  actor: string,
): Promise<StudioActionResult> {
  return post(`/v1/agent-studio/agent-versions/${versionId}/runs`, {
    project_id: projectId,
    actor,
    input: { objective, evidence_refs: ["agent-studio:operator-input"] },
    dedupe_key: `studio-ui:${versionId}:${await pressToken()}`,
  });
}

export async function runStudioPanel(
  projectId: string,
  versionId: string,
  objective: string,
  actor: string,
): Promise<StudioActionResult> {
  return post(`/v1/agent-studio/panel-versions/${versionId}/runs`, {
    project_id: projectId,
    actor,
    input: { objective, evidence_refs: ["agent-studio:operator-input"] },
    context: { initiated_from: "agent_studio_ui" },
    dedupe_key: `studio-panel-ui:${versionId}:${await pressToken()}`,
  });
}

export async function validateStudioWorkflow(
  projectId: string,
  spec: Record<string, unknown>,
  actor: string,
): Promise<StudioActionResult> {
  return post("/v1/agent-studio/workflows/validate", {
    project_id: projectId,
    actor,
    spec,
  });
}

export async function createStudioWorkflow(
  projectId: string,
  workflowKey: string,
  name: string,
  description: string,
  spec: Record<string, unknown>,
  actor: string,
): Promise<StudioActionResult> {
  const definition = await post("/v1/workflows", {
    project_id: projectId,
    workflow_key: workflowKey,
    name,
    description,
    actor,
  });
  if (!definition.ok || !definition.item?.id) return definition;
  const published = await post(
    `/v1/workflows/${encodeURIComponent(String(definition.item.id))}/versions`,
    {
      project_id: projectId,
      spec,
      activate: true,
      actor,
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
  actor: string,
): Promise<StudioActionResult> {
  return post(`/v1/workflows/${encodeURIComponent(definitionId)}/versions`, {
    project_id: projectId,
    spec,
    activate: true,
    actor,
  });
}

export async function runStudioWorkflow(
  projectId: string,
  versionId: string,
  objective: string,
  actor: string,
  startAnyway: string[] = [],
): Promise<StudioActionResult> {
  return post("/v1/workflow-runs", {
    project_id: projectId,
    workflow_version_id: versionId,
    input: { objective, evidence_refs: ["agent-studio:operator-input"] },
    policy_snapshot: { initiated_from: "agent_studio_ui" },
    dedupe_key: `studio-workflow-ui:${versionId}:${await pressToken()}`,
    actor,
    // Only the keys the operator ticked on the checklist a refusal drew.
    ...(startAnyway.length ? { start_anyway: startAnyway } : {}),
  });
}
