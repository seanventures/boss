import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";

/**
 * Merge-gate state for MANY previews in one call.
 *
 * The same defect as the app cards: every preview row fetched its own gate from a mount
 * effect through `previewGateAction`, and a Server Action re-renders the whole route to
 * reply. Nine preview rows meant nine renders of a page that takes tens of seconds.
 *
 * The gate has to be read before the button renders — the row says what approving will do and
 * refuses when it would do nothing — so the fetch cannot simply be dropped. It can be batched.
 */
export const dynamic = "force-dynamic";

/** Matches MAX_PER_REQUEST in lib/preview-gates.ts. Refused rather than truncated: a dropped
 *  preview would render no gate at all, which reads as "still loading" forever. */
const MAX_IDS = 60;

export async function GET(req: Request) {
  const raw = (new URL(req.url).searchParams.get("preview_ids") ?? "").trim();
  const ids = raw.split(",").map((s) => s.trim()).filter(Boolean);

  if (ids.length === 0) {
    return NextResponse.json({ error: "missing preview_ids" }, { status: 400 });
  }
  if (ids.length > MAX_IDS) {
    return NextResponse.json(
      { error: `too many preview_ids (${ids.length} > ${MAX_IDS}); request them in chunks` },
      { status: 400 },
    );
  }

  const results = await Promise.all(
    ids.map(async (id) => {
      try {
        const r = await fetch(`${API_URL}/v1/previews/${encodeURIComponent(id)}/gate`, {
          headers: authHeaders({}),
          cache: "no-store",
        });
        if (!r.ok) return [id, { error: `HTTP ${r.status}` }] as const;
        return [id, await r.json()] as const;
      } catch (e) {
        // One row's failure must not blank the other eight. The error rides along so the row
        // can say it could not check, rather than silently showing no gate.
        return [id, { error: String((e as Error)?.message ?? e) }] as const;
      }
    }),
  );

  return NextResponse.json({ gates: Object.fromEntries(results) });
}
