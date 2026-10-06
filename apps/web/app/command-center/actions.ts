"use server";

/**
 * Clean up (cc v2): the system doctor's safe fix for old stuck builds, in one press. The server
 * closes the questions about builds that already ended and stops any build that has not moved
 * for over a week, records each, and says what it did — which the row shows in place.
 */
import { revalidatePath } from "next/cache";

import { apiWrite } from "../../lib/api-write";
import { API_URL, authHeaders } from "../../lib/client";
import { declareAction } from "../../lib/identity/guarded-action";

export type CleanUpResult = { ok: boolean; result?: string; error?: string };

export async function cleanUpOldAction(form: FormData): Promise<CleanUpResult> {
  const projectId = String(form.get("project_id") ?? "").trim();
  const actor = String(form.get("actor") ?? "").trim();
  if (!projectId) return { ok: false, error: "No project." };
  try {
    const response = await apiWrite(`${API_URL}/v1/command-center/clean-up`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ project_id: projectId, ...(actor ? { actor } : {}) }),
    });
    const body = (await response.json().catch(() => ({}))) as { result?: string; detail?: string };
    if (!response.ok) return { ok: false, error: body.detail || `The API answered ${response.status}.` };
    revalidatePath("/");
    return { ok: true, result: body.result };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}

// THE PERMISSION EACH ACTION DECLARES (IDENTITY.md §6.5, R4; lib/identity/guarded-action.ts).
// A declaration for the console to mirror — disabled with the reason when not held — never a
// decision: the API checks every call. tests/identity-mirroring.test.ts keeps this table whole.
declareAction(cleanUpOldAction, "system:operate");
