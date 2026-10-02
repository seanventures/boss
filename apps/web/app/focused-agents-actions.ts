"use server";

import { revalidatePath } from "next/cache";

import { API_URL, authHeaders } from "../lib/client";
import { apiWrite } from "../lib/api-write";

export type FocusedAgentActionResult = {
  ok: boolean;
  status?: number;
  data?: Record<string, unknown>;
  error?: string;
};

async function readDetail(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: unknown };
    if (typeof payload.detail === "string") return payload.detail;
  } catch {
    /* not JSON */
  }
  return "";
}

async function post(
  path: string,
  body: Record<string, unknown>,
): Promise<FocusedAgentActionResult> {
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
          `Focused agent request failed (HTTP ${response.status}).`,
      };
    }
    revalidatePath("/");
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

export async function loadFocusedAgentsAction(
  projectId: string,
): Promise<FocusedAgentActionResult> {
  try {
    const response = await fetch(
      `${API_URL}/v1/focused-agents?project_id=${encodeURIComponent(projectId)}`,
      {
        headers: authHeaders({ "Content-Type": "application/json" }),
        cache: "no-store",
      },
    );
    if (!response.ok) {
      return {
        ok: false,
        status: response.status,
        error: `Focused agents unavailable (HTTP ${response.status}).`,
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

export async function createFocusedAgentAction(
  formData: FormData,
): Promise<FocusedAgentActionResult> {
  const projectId = String(formData.get("project_id") ?? "").trim();
  const actor = String(formData.get("actor") ?? "").trim();
  const kind = String(formData.get("kind") ?? "custom").trim() || "custom";
  const name = String(formData.get("name") ?? "").trim();
  const focus = String(formData.get("focus") ?? "").trim();
  const mode = String(formData.get("mode") ?? "always_on").trim() || "always_on";
  const interval = Number(formData.get("interval_seconds") ?? 120);
  const actions = formData.getAll("allowed_actions").map((item) => String(item));
  if (!projectId || !name || !focus) {
    return { ok: false, error: "name and focus are required." };
  }
  return post("/v1/focused-agents", {
    project_id: projectId,
    actor: actor || "executive_console",
    kind,
    name,
    focus,
    mode,
    interval_seconds: Number.isFinite(interval) ? interval : 120,
    allowed_actions: actions,
    start: true,
  });
}

export async function wakeFocusedAgentAction(
  formData: FormData,
): Promise<FocusedAgentActionResult> {
  const projectId = String(formData.get("project_id") ?? "").trim();
  const actor = String(formData.get("actor") ?? "").trim();
  const agentId = String(formData.get("agent_id") ?? "").trim();
  if (!projectId || !agentId) {
    return { ok: false, error: "agent_id is required." };
  }
  return post(`/v1/focused-agents/${encodeURIComponent(agentId)}/wake`, {
    project_id: projectId,
    actor: actor || "executive_console",
    reason: "manual",
  });
}

export async function pauseFocusedAgentAction(
  formData: FormData,
): Promise<FocusedAgentActionResult> {
  const projectId = String(formData.get("project_id") ?? "").trim();
  const actor = String(formData.get("actor") ?? "").trim();
  const agentId = String(formData.get("agent_id") ?? "").trim();
  if (!projectId || !agentId) {
    return { ok: false, error: "agent_id is required." };
  }
  return post(`/v1/focused-agents/${encodeURIComponent(agentId)}/pause`, {
    project_id: projectId,
    actor: actor || "executive_console",
  });
}

export async function activateFocusedAgentAction(
  formData: FormData,
): Promise<FocusedAgentActionResult> {
  const projectId = String(formData.get("project_id") ?? "").trim();
  const actor = String(formData.get("actor") ?? "").trim();
  const agentId = String(formData.get("agent_id") ?? "").trim();
  if (!projectId || !agentId) {
    return { ok: false, error: "agent_id is required." };
  }
  return post(`/v1/focused-agents/${encodeURIComponent(agentId)}/activate`, {
    project_id: projectId,
    actor: actor || "executive_console",
  });
}
