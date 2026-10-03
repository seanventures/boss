/**
 * The Workflows area's two calls to its proxy: read one view, and post one action.
 *
 * Shared by the area (delivery-workflows-ui.tsx) and the Runs composer, so both read refusals
 * the same way: a start refused for what the project lacks carries the readiness checklist.
 */
import { apiWrite } from "../../lib/api-write";
import type { Readiness } from "../workflow-readiness-ui";

export async function get<T>(view: string, projectId: string): Promise<T | { error: string }> {
  const params = new URLSearchParams({ view });
  if (projectId) params.set("project_id", projectId);
  try {
    const r = await fetch(`/api/delivery-workflows?${params}`, { cache: "no-store" });
    const body = await r.json();
    return r.ok ? body : { error: body?.error ?? `API returned ${r.status}` };
  } catch (e) {
    return { error: String((e as Error)?.message ?? e) };
  }
}

export /** POST one run action. `key` is the press's Idempotency-Key when a press sends it; without
 *  one (a background recheck) apiWrite mints its own and takes no press scope. */
async function post(action: string, payload: Record<string, unknown>, key?: string) {
  try {
    const r = await apiWrite("/api/delivery-workflows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, ...payload }),
    }, { key });
    const body = await r.json();
    // A start refused for what the project lacks carries the checklist, so the form can draw it.
    return r.ok ? { ok: true as const, data: body }
      : { ok: false as const, error: body?.error,
          readiness: (body?.readiness ?? undefined) as Readiness | undefined };
  } catch (e) {
    return { ok: false as const, error: String((e as Error)?.message ?? e) };
  }
}
