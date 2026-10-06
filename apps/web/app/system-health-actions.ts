"use server";

/**
 * A system-health card's press (stability spec §4d): one of the options the system doctor
 * offered, by key. The server does what the option's words say and answers what happened —
 * "Freed 1.2 GB · pressure 12% → 1%" — which the card shows in place.
 */
import { revalidatePath } from "next/cache";

import { apiWrite } from "../lib/api-write";
import { API_URL, authHeaders } from "../lib/client";
import { declareAction } from "../lib/identity/guarded-action";

export type SystemActResult = { ok: boolean; result?: string; error?: string; open?: string };

export async function actOnSystemFindingAction(form: FormData): Promise<SystemActResult> {
  const findingId = String(form.get("finding_id") ?? "").trim();
  const option = String(form.get("option") ?? "").trim();
  const actor = String(form.get("actor") ?? "").trim();
  if (!findingId || !option) return { ok: false, error: "Nothing to act on." };
  try {
    const response = await apiWrite(
      `${API_URL}/v1/system-doctor/findings/${encodeURIComponent(findingId)}/act`, {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ option, ...(actor ? { actor } : {}) }),
      });
    const body = (await response.json().catch(() => ({}))) as
      { result?: string; open?: string; detail?: string };
    if (!response.ok) return { ok: false, error: body.detail || `The API answered ${response.status}.` };
    revalidatePath("/");
    return { ok: true, result: body.result, ...(body.open ? { open: body.open } : {}) };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}

// THE PERMISSION EACH ACTION DECLARES (IDENTITY.md §6.5, R4; lib/identity/guarded-action.ts).
// A declaration for the console to mirror — disabled with the reason when not held — never a
// decision: the API checks every call. tests/identity-mirroring.test.ts keeps this table whole.
declareAction(actOnSystemFindingAction, "system:operate");
