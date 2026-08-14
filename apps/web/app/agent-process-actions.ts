"use server";

import { revalidatePath } from "next/cache";

import { API_URL, authHeaders } from "../lib/client";

export type AgentProcessActionResult = {
  ok: boolean;
  status?: number;
  data?: Record<string, unknown>;
  error?: string;
};

async function post(
  path: string,
  body: Record<string, unknown>,
): Promise<AgentProcessActionResult> {
  try {
    const response = await fetch(`${API_URL}${path}`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
      cache: "no-store",
    });
    if (!response.ok) {
      return {
        ok: false,
        status: response.status,
        error: `Agent process request failed (HTTP ${response.status}).`,
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

export async function startAgentProcessAction(
  formData: FormData,
): Promise<AgentProcessActionResult> {
  const projectId = String(formData.get("project_id") ?? "").trim();
  const actor = String(formData.get("actor") ?? "").trim();
  const prd = String(formData.get("prd") ?? "").trim();
  const title = String(formData.get("title") ?? "").trim();
  if (!projectId || !prd) {
    return { ok: false, error: "title/prd: paste a PRD to start the process." };
  }
  return post("/v1/agent-processes/runs", {
    project_id: projectId,
    actor: actor || "executive_console",
    title: title || "PRD delivery",
    prd,
    process_key: "prd_to_accepted_work",
    simulated: String(formData.get("simulated") ?? "") === "true",
    auto_build: String(formData.get("auto_build") ?? "") === "true",
  });
}

export async function loadAgentProcessRunsAction(
  projectId: string,
): Promise<AgentProcessActionResult> {
  try {
    const response = await fetch(
      `${API_URL}/v1/agent-processes/runs?project_id=${encodeURIComponent(projectId)}`,
      { headers: authHeaders({ "Content-Type": "application/json" }), cache: "no-store" },
    );
    if (!response.ok) {
      return { ok: false, status: response.status, error: `HTTP ${response.status}` };
    }
    return { ok: true, status: response.status, data: (await response.json()) as Record<string, unknown> };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "request failed" };
  }
}
