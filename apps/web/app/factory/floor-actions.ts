"use server";

/**
 * The Floor's presses (factory spec §4.2.1, §4.1): retry a build, waive the checks it failed,
 * start a direct change. Each returns what happened in the operator's words; the client calls
 * them through callAction, so a double press is one request.
 */
import { BossApiError } from "@boss/api-client";
import { revalidatePath } from "next/cache";

import { client, PROJECT_ID, resolveProjectId } from "../../lib/client";

export type FloorResult = { ok: boolean; error?: string; runId?: string; attempt?: number; url?: string; workflowRunId?: string };

function said(e: unknown): string {
  if (e instanceof BossApiError) return e.message || `The API answered ${e.status}.`;
  return e instanceof Error ? e.message : String(e);
}

function field(form: FormData, key: string): string {
  const v = form.get(key);
  return typeof v === "string" ? v.trim() : "";
}

/** Retry: a new attempt on the same branch (POST /v1/code-tasks/{id}/retry). */
export async function retryBuildAction(form: FormData): Promise<FloorResult> {
  const runId = field(form, "run_id");
  if (!runId) return { ok: false, error: "No build to retry." };
  try {
    const res = await client.retryCodeTask(runId, field(form, "fix_note") || undefined,
      field(form, "actor") || "executive_console");
    revalidatePath("/");
    return { ok: true, runId: res?.run_id, attempt: res?.attempt };
  } catch (e) {
    return { ok: false, error: said(e) };
  }
}

/** Waive the named failing checks and open the PR. The merge still needs the operator. */
export async function waiveChecksAction(form: FormData): Promise<FloorResult> {
  const runId = field(form, "run_id");
  const reason = field(form, "reason");
  const gates = form.getAll("gate").map(String).filter(Boolean);
  if (!runId) return { ok: false, error: "No build to waive." };
  if (!reason) return { ok: false, error: "Say why. The reason is recorded." };
  if (!gates.length) return { ok: false, error: "Pick a check to waive." };
  try {
    const res = await client.overrideCodeTask(runId, gates, reason, field(form, "actor") || "executive_console");
    revalidatePath("/");
    // The API answers 200 with `status: "error"` when the PR could not be opened.
    const out = (res ?? {}) as { status?: string; detail?: string; pull_request_url?: string };
    if (out.status === "error") return { ok: false, error: out.detail || "The PR could not be opened." };
    return { ok: true, url: out.pull_request_url };
  } catch (e) {
    return { ok: false, error: said(e) };
  }
}

/** A direct change: an already-specified edit, straight to a build (no PRD). */
export async function startDirectChangeAction(form: FormData): Promise<FloorResult> {
  const title = field(form, "title");
  const instructions = field(form, "instructions");
  if (!title || !instructions) return { ok: false, error: "Name it and say what to change." };
  const lines = (k: string) => field(form, k).split("\n").map((s) => s.trim()).filter(Boolean);
  try {
    const res = await client.createCodeTask({
      project_id: field(form, "project_id") || resolveProjectId(PROJECT_ID),
      title, instructions,
      acceptance_criteria: lines("acceptance_criteria"),
      test_commands: lines("test_commands"),
      // The review panel's size (walk D13): 1 reviewer, or a panel whose seats each sign.
      ...(Number(field(form, "review_panel_size")) > 0 ? { review_panel_size: Number(field(form, "review_panel_size")) } : {}),
      owner: field(form, "owner") || undefined,
      repo: field(form, "repo") || undefined,
      base_branch: field(form, "base_branch") || undefined,
    });
    revalidatePath("/");
    return { ok: true, runId: res?.run_id, workflowRunId: res?.workflow_run_id ?? undefined };
  } catch (e) {
    return { ok: false, error: said(e) };
  }
}
