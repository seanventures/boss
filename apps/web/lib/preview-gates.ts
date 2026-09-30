import { createBatcher } from "./batch-fetch";

/**
 * Merge-gate state for preview rows — one request for all of them.
 *
 * Was `previewGateAction(preview.id)` in a per-row mount effect, which re-rendered the whole
 * route once per row. Nine rows, nine renders. Same fix as the app cards; see batch-fetch.ts.
 */

export type PreviewGate = {
  can_approve?: boolean;
  decision_status?: string | null;
  blockers?: string[];
  error?: string;
};

const batcher = createBatcher<PreviewGate>({
  url: (ids) => `/api/preview-gates?preview_ids=${encodeURIComponent(ids.join(","))}`,
  pick: (body, id) => (body as { gates?: Record<string, PreviewGate> })?.gates?.[id],
  ttlMs: 2_500,
  maxPerRequest: 60,
});

/** Never rejects: resolves with `{ error }` so a failed lookup cannot be mistaken for a row
 *  that has no gate — those render differently and mean different things. */
export function loadPreviewGate(previewId: string): Promise<PreviewGate> {
  const id = (previewId ?? "").trim();
  if (!id) return Promise.resolve({ error: "missing preview id" });
  return batcher.load(id);
}

/** After approving, the cached gate describes the world before the approval. */
export function forgetPreviewGate(previewId: string): void {
  batcher.forget(previewId);
}

/** Test seam: reset module state between cases. */
export function __resetPreviewGateCache(): void {
  batcher.reset();
}
