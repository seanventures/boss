"use server";

import { revalidatePath } from "next/cache";
import { API_URL, authHeaders, resolveProjectId } from "../lib/client";
import { apiWrite } from "../lib/api-write";

/**
 * Set one stage's per-call token cap from the Govern screen.
 *
 * The token recommendations told an operator to "raise that stage's max_total_tokens_per_call
 * via PUT /v1/token-efficiency/policies" (console walk, 2026-10-02) — an instruction nobody in a
 * browser can follow. This is the button that instruction was standing in for.
 */
export type TokenCapResult = { ok: true; stage: string; cap: number } | { ok: false; error: string };

export async function setStageTokenCapAction(
  _previous: TokenCapResult | null,
  formData: FormData,
): Promise<TokenCapResult> {
  const projectId = resolveProjectId(String(formData.get("project_id") ?? ""));
  const stage = String(formData.get("stage") ?? "").trim();
  const cap = Number(String(formData.get("max_total_tokens_per_call") ?? "").trim());
  if (!stage) return { ok: false, error: "Pick the stage whose cap you are changing." };
  if (!Number.isFinite(cap) || cap < 1000) {
    return { ok: false, error: "Enter a per-call cap of at least 1,000 tokens." };
  }
  try {
    const response = await apiWrite(`${API_URL}/v1/token-efficiency/policies`, {
      method: "PUT",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        project_id: projectId,
        policies: [{ stage, max_total_tokens_per_call: Math.trunc(cap) }],
      }),
    });
    if (!response.ok) {
      const text = (await response.text()).slice(0, 240);
      return {
        ok: false,
        error: `The cap was not saved: the API answered ${response.status}${text ? ` (${text})` : ""}. Nothing changed; check the value and save again.`,
      };
    }
    // The policy card beside this form lists the per-stage caps; without a revalidate it kept
    // showing the old cap next to "Saved: ... capped at N" until the next navigation.
    revalidatePath("/");
    return { ok: true, stage, cap: Math.trunc(cap) };
  } catch (error) {
    return {
      ok: false,
      error: `The cap was not saved: ${error instanceof Error ? error.message : "the API could not be reached"}. Check that the BOSS API is running, then save again.`,
    };
  }
}
