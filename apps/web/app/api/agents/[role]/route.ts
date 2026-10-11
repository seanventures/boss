import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../../lib/client";
import { apiErrorText } from "../../../../lib/api-error";
import { rawRequestUrl } from "../../../../lib/raw-request-url";

/** One agent's detail. The roster card had nowhere to go; this is what is behind it now. */
export const dynamic = "force-dynamic";

export async function GET(req: Request, ctx: { params: Promise<{ role: string }> }) {
  const { role } = await ctx.params;
  const projectId = rawRequestUrl(req).searchParams.get("project_id")?.trim();
  if (!role) return NextResponse.json({ error: "missing role" }, { status: 400 });
  if (!projectId) return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  try {
    const r = await fetch(
      `${API_URL}/v1/console/v2/agents/${encodeURIComponent(role)}`
      + `?project_id=${encodeURIComponent(projectId)}`,
      { headers: authHeaders({}), cache: "no-store" });
    const raw = await r.text();
    let parsed: unknown;
    try { parsed = JSON.parse(raw); } catch { parsed = { detail: raw }; }
    if (!r.ok) {
      return NextResponse.json(
        { error: apiErrorText(parsed, r.status) },
        { status: r.status });
    }
    return NextResponse.json(parsed);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
