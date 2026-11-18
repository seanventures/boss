import { NextResponse } from "next/server";

import { describeApiError } from "../../../../lib/api-error";
import { apiWrite } from "../../../../lib/api-write";
import { API_URL, authHeaders } from "../../../../lib/client";

/** Easy Mode's one-click build (BOSSAPP-1273): /v1/factory/launch with the composer's JSON. */
export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  const body = await req.json().catch(() => null);
  if (!body || typeof body !== "object") {
    return NextResponse.json({ error: "describe what to build first" }, { status: 400 });
  }
  try {
    const r = await apiWrite(`${API_URL}/v1/factory/launch`, {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body), cache: "no-store",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return NextResponse.json({ ...(data as object), error: describeApiError(data, r.status) }, { status: r.status });
    return NextResponse.json(data, { status: r.status });
  } catch (e) {
    return NextResponse.json({ error: `The build did not reach BOSS: ${(e as Error).message}` }, { status: 502 });
  }
}
