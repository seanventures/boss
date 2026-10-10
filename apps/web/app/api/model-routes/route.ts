import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { apiErrorText } from "../../../lib/api-error";
import { rawRequestUrl } from "../../../lib/raw-request-url";

/**
 * "Why this model" for one run, thread or meeting: GET /v1/model-routes (the model router's
 * decisions, docs/product/MODEL_ROUTER.md). Read-only; the two ids are the only inputs.
 */
export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const url = rawRequestUrl(req);
  const projectId = (url.searchParams.get("project_id") ?? "").trim();
  const unitId = (url.searchParams.get("unit_id") ?? "").trim();
  if (!projectId || !unitId) {
    return NextResponse.json({ error: "project_id and unit_id are required" }, { status: 400 });
  }
  try {
    const r = await fetch(`${API_URL}/v1/model-routes?project_id=${encodeURIComponent(projectId)}`
      + `&unit_id=${encodeURIComponent(unitId)}`, {
      headers: authHeaders({ "Content-Type": "application/json" }), cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return NextResponse.json({ error: apiErrorText(data, r.status) }, { status: r.status });
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
