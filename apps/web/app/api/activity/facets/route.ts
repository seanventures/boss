import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../../lib/client";

/**
 * Per-level and per-stream counts for the whole Activity log (GET /v1/activity/facets).
 *
 * A route of its own rather than a field on the list: classifying levels means reading up to a
 * few thousand rows per stream, and the first page of the log must not wait for that. The chips
 * fill in when this answers.
 */
export const dynamic = "force-dynamic";

const FORWARD = ["project_id", "since", "until", "run_id", "workflow_run_id", "step_id", "agent", "service"] as const;

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
    const r = await fetch(`${API_URL}/v1/activity/facets?${params.toString()}`, {
      headers: authHeaders({ "Content-Type": "application/json" }),
      cache: "no-store",
    });
    if (!r.ok) {
      return NextResponse.json({ error: `API returned ${r.status}` }, { status: r.status });
    }
    return NextResponse.json(await r.json());
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
