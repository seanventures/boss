import { NextResponse } from "next/server";

import { describeApiError } from "../../../../lib/api-error";
import { API_URL, authHeaders } from "../../../../lib/client";

/**
 * The template gallery on the build home (BOSSAPP-1273): /v1/factory/templates, read-only.
 * The console called /api/factory/templates from the day the gallery shipped but no route
 * existed, so the home always said "Templates did not load." (operator, 2026-11-18).
 */
export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const r = await fetch(`${API_URL}/v1/factory/templates`, { headers: authHeaders(), cache: "no-store" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return NextResponse.json({ error: describeApiError(data, r.status) }, { status: r.status });
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: `Templates did not load: ${(e as Error).message}` }, { status: 502 });
  }
}
