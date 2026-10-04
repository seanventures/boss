import type { DiscoveryRun } from "@boss/api-client";

/**
 * One dossier, fetched when it is opened. The list carries summaries only (lib/list-budget.ts);
 * this is the DETAIL, through the console's same-origin proxy (app/v1/[...path]).
 */
export type DiscoveryDetailResult =
  | { ok: true; run: DiscoveryRun }
  | { ok: false; error: string };

export async function fetchDiscoveryDetail(
  runId: string,
  fetcher: typeof fetch = fetch,
): Promise<DiscoveryDetailResult> {
  try {
    const r = await fetcher(`/v1/discoveries/${encodeURIComponent(runId)}`, { cache: "no-store" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const detail = (body as { detail?: unknown }).detail;
      return { ok: false, error: typeof detail === "string" ? detail : `HTTP ${r.status}` };
    }
    return { ok: true, run: body as DiscoveryRun };
  } catch {
    return { ok: false, error: "the control plane did not answer" };
  }
}
