import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { apiErrorText } from "../../../lib/api-error";
import { rawRequestUrl } from "../../../lib/raw-request-url";
import { TOOL_CALL_FORWARD } from "../../../lib/tool-calls";

/**
 * One page of the tool-call audit (GET /v1/tool-calls, BOSSAPP-819): JSON out, same origin.
 * Only the filters the API understands are forwarded (a typo cannot widen a query).
 */
export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const incoming = rawRequestUrl(req).searchParams;
  if (!incoming.get("project_id")?.trim()) {
    return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  }
  const params = new URLSearchParams();
  for (const key of TOOL_CALL_FORWARD) {
    const value = incoming.get(key)?.trim();
    if (value) params.set(key, value);
  }
  try {
    const r = await fetch(`${API_URL}/v1/tool-calls?${params.toString()}`, {
      headers: authHeaders({ "Content-Type": "application/json" }), cache: "no-store",
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return NextResponse.json({ error: apiErrorText(body, r.status) }, { status: r.status });
    return NextResponse.json(body, { headers: { "Cache-Control": "no-cache" } });
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
