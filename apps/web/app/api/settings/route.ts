import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite } from "../../../lib/api-write";
import { apiErrorText } from "../../../lib/api-error";

/** Route handler rather than a server action: the console takes seconds to re-render, and a
 *  knob that appears to hang while the value has already been written reads as broken. */
export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const projectId = new URL(req.url).searchParams.get("project_id")?.trim();
  if (!projectId) return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  try {
    const r = await fetch(`${API_URL}/v1/settings?project_id=${encodeURIComponent(projectId)}`,
                          { headers: authHeaders(), cache: "no-store" });
    const body = await r.json().catch(() => ({}));
    return NextResponse.json(body, { status: r.ok ? 200 : r.status });
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}

export async function PUT(req: Request) {
  let body: Record<string, unknown>;
  try { body = await req.json(); }
  catch { return NextResponse.json({ error: "invalid JSON body" }, { status: 400 }); }
  const key = String(body.key ?? "").trim();
  if (!key) return NextResponse.json({ error: "key is required" }, { status: 400 });
  try {
    const r = await apiWrite(`${API_URL}/v1/settings/${encodeURIComponent(key)}`, {
      method: "PUT",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
      cache: "no-store",
    });
    const parsed = await r.json().catch(() => ({}));
    // The API's own message, forwarded: "has to be between 1 and 16" is actionable.
    if (!r.ok) {
      return NextResponse.json({ error: apiErrorText(parsed, r.status) }, { status: r.status });
    }
    return NextResponse.json(parsed);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
