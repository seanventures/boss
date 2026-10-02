"use server";

import { revalidatePath } from "next/cache";

import { API_URL, authHeaders } from "../lib/client";
import { apiWrite } from "../lib/api-write";

export type AgentProcessActionResult = {
  ok: boolean;
  status?: number;
  data?: Record<string, unknown>;
  error?: string;
};

async function readDetail(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: unknown };
    if (typeof payload.detail === "string") return payload.detail;
    if (Array.isArray(payload.detail)) {
      return payload.detail
        .map((item) =>
          typeof item === "object" && item && "msg" in item
            ? String((item as { msg: unknown }).msg)
            : String(item),
        )
        .join("; ");
    }
  } catch {
    /* not JSON */
  }
  return "";
}

async function post(
  path: string,
  body: Record<string, unknown>,
  options?: { revalidate?: boolean },
): Promise<AgentProcessActionResult> {
  try {
    const response = await apiWrite(`${API_URL}${path}`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
      cache: "no-store",
    });
    if (!response.ok) {
      return {
        ok: false,
        status: response.status,
        error:
          (await readDetail(response)) ||
          `Agent process request failed (HTTP ${response.status}).`,
      };
    }
    if (options?.revalidate !== false) {
      revalidatePath("/");
    }
    return {
      ok: true,
      status: response.status,
      data: (await response.json()) as Record<string, unknown>,
    };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : "request failed",
    };
  }
}

async function get(path: string): Promise<AgentProcessActionResult> {
  try {
    const response = await fetch(`${API_URL}${path}`, {
      headers: authHeaders({ "Content-Type": "application/json" }),
      cache: "no-store",
    });
    if (!response.ok) {
      return {
        ok: false,
        status: response.status,
        error:
          (await readDetail(response)) ||
          `Agent process request failed (HTTP ${response.status}).`,
      };
    }
    return {
      ok: true,
      status: response.status,
      data: (await response.json()) as Record<string, unknown>,
    };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : "request failed",
    };
  }
}

export async function startAgentProcessAction(
  formData: FormData,
): Promise<AgentProcessActionResult> {
  const projectId = String(formData.get("project_id") ?? "").trim();
  const actor = String(formData.get("actor") ?? "").trim();
  const prd = String(formData.get("prd") ?? "").trim();
  const title = String(formData.get("title") ?? "").trim();
  const processKey = String(formData.get("process_key") ?? "").trim();
  if (!projectId || !prd) {
    return { ok: false, error: "title/prd: paste a PRD to start the process." };
  }
  return post("/v1/agent-processes/runs", {
    project_id: projectId,
    actor: actor || "executive_console",
    title: title || "PRD delivery",
    prd,
    process_key: processKey || "prd_to_accepted_work",
    simulated: String(formData.get("simulated") ?? "") === "true",
    auto_build: String(formData.get("auto_build") ?? "") === "true",
  });
}

export async function compileAgentProcessAction(
  formData: FormData,
): Promise<AgentProcessActionResult> {
  const projectId = String(formData.get("project_id") ?? "").trim();
  const actor = String(formData.get("actor") ?? "").trim();
  const description = String(formData.get("description") ?? "").trim();
  if (!projectId || !description) {
    return { ok: false, error: "Describe the handoff chain to compile a preview." };
  }
  return post(
    "/v1/agent-processes/compile",
    {
      project_id: projectId,
      actor: actor || "executive_console",
      description,
    },
    { revalidate: false },
  );
}

export async function saveAgentProcessTemplateAction(
  formData: FormData,
): Promise<AgentProcessActionResult> {
  const projectId = String(formData.get("project_id") ?? "").trim();
  const actor = String(formData.get("actor") ?? "").trim();
  const processKey = String(formData.get("process_key") ?? "").trim();
  const name = String(formData.get("name") ?? "").trim();
  const description = String(formData.get("description") ?? "").trim();
  const specRaw = String(formData.get("spec") ?? "").trim();
  if (!projectId || !name) {
    return { ok: false, error: "name is required to save a process template." };
  }
  let spec: Record<string, unknown> = {};
  if (specRaw) {
    try {
      const parsed = JSON.parse(specRaw) as unknown;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        return { ok: false, error: "Process spec must be an object." };
      }
      spec = parsed as Record<string, unknown>;
    } catch {
      return { ok: false, error: "Process spec is invalid JSON." };
    }
  }
  if (!Array.isArray(spec.steps) || spec.steps.length === 0) {
    return { ok: false, error: "Compile or add at least one step before saving." };
  }
  return post("/v1/agent-processes/templates", {
    project_id: projectId,
    actor: actor || "executive_console",
    process_key: processKey || "custom",
    name,
    description,
    spec,
  });
}

export async function loadAgentProcessTemplatesAction(
  projectId: string,
): Promise<AgentProcessActionResult> {
  return get(
    `/v1/agent-processes/templates?project_id=${encodeURIComponent(projectId)}`,
  );
}

export async function loadAgentProcessRunsAction(
  projectId: string,
): Promise<AgentProcessActionResult> {
  return get(
    `/v1/agent-processes/runs?project_id=${encodeURIComponent(projectId)}`,
  );
}

export async function loadAgentProcessRunAction(
  projectId: string,
  runId: string,
): Promise<AgentProcessActionResult> {
  const id = runId.trim();
  if (!projectId.trim() || !id) {
    return { ok: false, error: "run id is required." };
  }
  return get(
    `/v1/agent-processes/runs/${encodeURIComponent(id)}?project_id=${encodeURIComponent(projectId)}`,
  );
}
