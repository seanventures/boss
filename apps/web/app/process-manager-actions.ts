"use server";

import { revalidatePath } from "next/cache";

import { API_URL, authHeaders } from "../lib/client";
import { apiWrite } from "../lib/api-write";
import { declareAction } from "../lib/identity/guarded-action";

export type ProcessManagerActionResult = {
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

async function get(
  path: string,
): Promise<ProcessManagerActionResult> {
  try {
    const response = await fetch(`${API_URL}${path}`, {
      headers: authHeaders({ "Content-Type": "application/json" }),
      cache: "no-store",
    });
    if (!response.ok) {
      const detail = await readDetail(response);
      return {
        ok: false,
        status: response.status,
        error:
          detail ||
          (response.status === 503
            ? "Process manager runtime is unavailable."
            : `Process manager unavailable (HTTP ${response.status}).`),
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

async function post(
  path: string,
  body: Record<string, unknown>,
): Promise<ProcessManagerActionResult> {
  try {
    const response = await apiWrite(`${API_URL}${path}`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
      cache: "no-store",
    });
    if (!response.ok) {
      const detail = await readDetail(response);
      return {
        ok: false,
        status: response.status,
        error:
          detail ||
          (response.status === 503
            ? "Process manager runtime is unavailable."
            : `Process manager request failed (HTTP ${response.status}).`),
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

export async function loadProcessManagerAction(
  projectId: string,
): Promise<ProcessManagerActionResult> {
  if (!projectId.trim()) {
    return { ok: false, error: "project_id is required." };
  }
  return get(
    `/v1/process-manager?project_id=${encodeURIComponent(projectId)}`,
  );
}

export async function loadProcessManagerMetricsAction(
  projectId: string,
): Promise<ProcessManagerActionResult> {
  if (!projectId.trim()) {
    return { ok: false, error: "project_id is required." };
  }
  return get(
    `/v1/process-manager/metrics?project_id=${encodeURIComponent(projectId)}`,
  );
}

export async function loadProcessManagerProposalsAction(
  projectId: string,
): Promise<ProcessManagerActionResult> {
  if (!projectId.trim()) {
    return { ok: false, error: "project_id is required." };
  }
  return get(
    `/v1/process-manager/proposals?project_id=${encodeURIComponent(projectId)}`,
  );
}

export async function tickProcessManagerAction(
  formData: FormData,
): Promise<ProcessManagerActionResult> {
  const projectId = String(formData.get("project_id") ?? "").trim();
  const actor = String(formData.get("actor") ?? "").trim();
  if (!projectId) {
    return { ok: false, error: "project_id is required." };
  }
  return post("/v1/process-manager/tick", {
    project_id: projectId,
    actor: actor || "executive_console",
  });
}

// THE PERMISSION EACH ACTION DECLARES (IDENTITY.md §6.5, R4; lib/identity/guarded-action.ts).
// A declaration for the console to mirror — disabled with the reason when not held — never a
// decision: the API checks every call. tests/identity-mirroring.test.ts keeps this table whole.
declareAction(loadProcessManagerAction, "autopilot:read");
declareAction(loadProcessManagerMetricsAction, "autopilot:read");
declareAction(loadProcessManagerProposalsAction, "autopilot:read");
declareAction(tickProcessManagerAction, "autopilot:operate");
