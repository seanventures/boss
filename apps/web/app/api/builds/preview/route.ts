import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../../lib/client";
import { refusalFrom } from "../../../../lib/start-refusal";

export const dynamic = "force-dynamic";

/** Read-only POST keeps the private brief out of the URL. No write intent is forwarded. */
export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try { body = await req.json(); }
  catch { return NextResponse.json({ error: "invalid JSON body" }, { status: 400 }); }
  try {
    const r = await fetch(`${API_URL}/v1/delivery-workflows/agreement`, {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body), cache: "no-store",
    });
    const data = await r.json().catch(() => ({}));
    return NextResponse.json(r.ok ? data : refusalFrom(r.status, data), { status: r.status });
  } catch (error) {
    return NextResponse.json({ error: (error as Error)?.message ?? "The agreement could not be read." }, { status: 502 });
  }
}
