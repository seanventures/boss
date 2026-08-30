import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";

/**
 * The activity log, as a route handler rather than a Server Action.
 *
 * This one was the single most expensive thing left on the page. Measured on the live console
 * after the per-card storm was fixed: fifteen server actions on load, fourteen of them under
 * 300ms, and this one at 25.3 seconds.
 *
 * Two costs stacked. `/v1/activity` itself took 14.3s (it loaded 46,774 rows to display 50 —
 * fixed separately in activity_log.py), and the Server Action reply added a full re-render of a
 * route that takes ten seconds to render. A route handler removes the second cost entirely:
 * JSON out, no re-render, same origin, same auth.
 *
 * Search params are forwarded rather than reconstructed, so the filter vocabulary lives in one
 * place — the API — instead of being duplicated and drifting here.
 */
export const dynamic = "force-dynamic";

/** Everything /v1/activity understands. An unknown param is dropped rather than forwarded, so
 *  a typo cannot silently widen a query the operator thinks they narrowed. */
const FORWARD = [
  "project_id", "q", "kind", "status", "actor", "entity_type",
  // The facets the log viewer reasons in: how loud, which stream, which work task, which AI
  // agent. A param missing from this list is dropped silently, so adding a filter to the UI
  // without adding it here narrows nothing and looks like the filter is broken.
  // ...and which BOSS process said it (api | worker), which is how a container log gets
  // separated from the run it happened during.
  "level", "source", "run_id", "agent", "service",
  "since", "until", "sort", "order", "limit", "offset",
] as const;

export async function GET(req: Request) {
  const incoming = new URL(req.url).searchParams;
  if (!incoming.get("project_id")?.trim()) {
    return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  }

  const params = new URLSearchParams();
  for (const key of FORWARD) {
    const value = incoming.get(key)?.trim();
    if (value) params.set(key, value);
  }

  try {
    const r = await fetch(`${API_URL}/v1/activity?${params.toString()}`, {
      headers: authHeaders({ "Content-Type": "application/json" }),
      cache: "no-store",
    });
    if (!r.ok) {
      return NextResponse.json({ error: `API returned ${r.status}` }, { status: r.status });
    }
    return NextResponse.json(await r.json());
  } catch (e) {
    return NextResponse.json(
      { error: String((e as Error)?.message ?? e) },
      { status: 502 },
    );
  }
}
