"use server";

/**
 * An Infra card's press (infra-observability spec §2.2): `Roll back` on a failed deploy,
 * `Re-verify` on an unreachable host. The server does what the option says (the same routes the
 * Deploys rail and the host drawer post) and answers what happened, which the card shows in
 * place. `Open` is not a press: the card follows the item's `open` href.
 */
import { revalidatePath } from "next/cache";

import { apiWrite } from "../../lib/api-write";
import { API_URL, authHeaders } from "../../lib/client";

export type InfraActResult = { ok: boolean; result?: string; error?: string; open?: string };

export async function actOnInfraAttentionAction(form: FormData): Promise<InfraActResult> {
  const id = String(form.get("id") ?? "").trim();
  const option = String(form.get("option") ?? "").trim();
  const actor = String(form.get("actor") ?? "").trim();
  const projectId = String(form.get("project_id") ?? "").trim();
  if (!id || !option || !projectId) return { ok: false, error: "Nothing to act on." };
  try {
    const response = await apiWrite(`${API_URL}/v1/infra/attention/act`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ project_id: projectId, id, option, ...(actor ? { actor } : {}) }),
    });
    const body = (await response.json().catch(() => ({}))) as { result?: string; open?: string; detail?: unknown };
    if (!response.ok) {
      const detail = typeof body.detail === "string" ? body.detail : `The API answered ${response.status}.`;
      return { ok: false, error: detail };
    }
    revalidatePath("/");
    return { ok: true, result: body.result, ...(body.open ? { open: body.open } : {}) };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}
