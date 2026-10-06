"use server";

import { revalidatePath } from "next/cache";

import { API_URL, authHeaders } from "../lib/client";
import {
  describeMetatronCaptainUnavailable,
  isCaptainUnavailableStatus,
} from "../lib/metatron-captain";
import { apiWrite } from "../lib/api-write";
import { declareAction } from "../lib/identity/guarded-action";

export type MetatronCaptainActionResult = {
  ok: boolean;
  status?: number;
  unavailable?: boolean;
  data?: Record<string, unknown>;
  error?: string;
};

function fail(
  status: number | undefined,
  detail?: string,
): MetatronCaptainActionResult {
  return {
    ok: false,
    status,
    unavailable: isCaptainUnavailableStatus(status) || !status,
    error: describeMetatronCaptainUnavailable(status, detail),
  };
}

async function readDetail(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: unknown };
    if (typeof payload.detail === "string") return payload.detail;
    if (Array.isArray(payload.detail)) {
      return payload.detail
        .map((item) =>
          item && typeof item === "object" && "msg" in item
            ? String((item as { msg: unknown }).msg)
            : String(item),
        )
        .join("; ");
    }
  } catch {
    /* body was not JSON */
  }
  return "";
}

async function captainGet(path: string): Promise<MetatronCaptainActionResult> {
  try {
    const response = await fetch(`${API_URL}${path}`, {
      headers: authHeaders({ "Content-Type": "application/json" }),
      cache: "no-store",
    });
    if (!response.ok) {
      return fail(response.status, await readDetail(response));
    }
    return {
      ok: true,
      status: response.status,
      data: (await response.json()) as Record<string, unknown>,
    };
  } catch (error) {
    return fail(undefined, error instanceof Error ? error.message : "request failed");
  }
}

async function captainPost(
  path: string,
  body: Record<string, unknown>,
  revalidate = true,
): Promise<MetatronCaptainActionResult> {
  try {
    const response = await apiWrite(`${API_URL}${path}`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
      cache: "no-store",
    });
    if (!response.ok) {
      return fail(response.status, await readDetail(response));
    }
    if (revalidate) {
      revalidatePath("/");
    }
    return {
      ok: true,
      status: response.status,
      data: (await response.json()) as Record<string, unknown>,
    };
  } catch (error) {
    return fail(undefined, error instanceof Error ? error.message : "request failed");
  }
}

export async function loadMetatronCaptainSessionAction(
  projectId: string,
  actor: string,
): Promise<MetatronCaptainActionResult> {
  const project = encodeURIComponent(projectId);
  const who = encodeURIComponent(actor);
  return captainGet(`/v1/metatron/session?project_id=${project}&actor=${who}`);
}

export async function sendMetatronCaptainMessageAction(
  formData: FormData,
): Promise<MetatronCaptainActionResult> {
  const projectId = String(formData.get("project_id") ?? "").trim();
  const actor = String(formData.get("actor") ?? "").trim();
  const message = String(formData.get("message") ?? "").trim();
  const threadId = String(formData.get("thread_id") ?? "").trim();
  if (!projectId || !message) {
    return {
      ok: false,
      error: "project_id and message are required.",
    };
  }
  return captainPost("/v1/metatron/session", {
    project_id: projectId,
    actor: actor || "executive_console",
    message,
    thread_id: threadId || undefined,
  });
}

export async function createMetatronCaptainGoalAction(
  formData: FormData,
): Promise<MetatronCaptainActionResult> {
  const projectId = String(formData.get("project_id") ?? "").trim();
  const actor = String(formData.get("actor") ?? "").trim();
  const title = String(formData.get("title") ?? "").trim();
  const kind = String(formData.get("kind") ?? "custom").trim() || "custom";
  const description = String(formData.get("description") ?? "").trim();
  if (!projectId || !title) {
    return {
      ok: false,
      error: "project_id and title are required.",
    };
  }
  return captainPost("/v1/metatron/goals", {
    project_id: projectId,
    actor: actor || "executive_console",
    title,
    kind,
    description,
  });
}

export async function refreshMetatronCaptainWatchAction(
  formData: FormData,
): Promise<MetatronCaptainActionResult> {
  const projectId = String(formData.get("project_id") ?? "").trim();
  const actor = String(formData.get("actor") ?? "").trim();
  if (!projectId) {
    return { ok: false, error: "project_id is required." };
  }
  return captainPost("/v1/metatron/watch", {
    project_id: projectId,
    actor: actor || "executive_console",
  });
}

export async function proposeMetatronCaptainDriveAction(
  formData: FormData,
): Promise<MetatronCaptainActionResult> {
  const projectId = String(formData.get("project_id") ?? "").trim();
  const actor = String(formData.get("actor") ?? "").trim();
  if (!projectId) {
    return { ok: false, error: "project_id is required." };
  }
  const auto = String(formData.get("auto") ?? "").trim() === "true";
  return captainPost("/v1/metatron/drive", {
    project_id: projectId,
    actor: actor || "executive_console",
    auto,
  });
}

export async function runMetatronCaptainOverwatchAction(
  formData: FormData,
): Promise<MetatronCaptainActionResult> {
  const projectId = String(formData.get("project_id") ?? "").trim();
  const actor = String(formData.get("actor") ?? "").trim();
  if (!projectId) {
    return { ok: false, error: "project_id is required." };
  }
  return captainPost("/v1/metatron/overwatch", {
    project_id: projectId,
    actor: actor || "executive_console",
  });
}

export async function loadMetatronTheaterAction(
  projectId: string,
  actor: string,
): Promise<MetatronCaptainActionResult> {
  const project = encodeURIComponent(projectId);
  const who = encodeURIComponent(actor);
  return captainGet(`/v1/metatron/theater?project_id=${project}&actor=${who}`);
}

export async function runMetatronTheaterAction(
  formData: FormData,
): Promise<MetatronCaptainActionResult> {
  const projectId = String(formData.get("project_id") ?? "").trim();
  const actor = String(formData.get("actor") ?? "").trim();
  if (!projectId) {
    return { ok: false, error: "project_id is required." };
  }
  return captainPost("/v1/metatron/theater", {
    project_id: projectId,
    actor: actor || "executive_console",
    auto: true,
  });
}

// THE PERMISSION EACH ACTION DECLARES (IDENTITY.md §6.5, R4; lib/identity/guarded-action.ts).
// A declaration for the console to mirror — disabled with the reason when not held — never a
// decision: the API checks every call. tests/identity-mirroring.test.ts keeps this table whole.
declareAction(loadMetatronCaptainSessionAction, "assistant:chat");
declareAction(sendMetatronCaptainMessageAction, "assistant:chat");
declareAction(createMetatronCaptainGoalAction, "autopilot:configure");
declareAction(refreshMetatronCaptainWatchAction, "autopilot:read");
declareAction(proposeMetatronCaptainDriveAction, "autopilot:operate");
declareAction(runMetatronCaptainOverwatchAction, "autopilot:operate");
declareAction(loadMetatronTheaterAction, "autopilot:read");
declareAction(runMetatronTheaterAction, "autopilot:operate");
