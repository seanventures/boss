import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";

/**
 * Convene the PRD panel without re-rendering the console.
 *
 * Same reason as /api/discoveries: as a Server Action this waited on a full 20-27s route
 * re-render before returning, so pressing Build looked like nothing had happened.
 */
export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const projectId = String(body.project_id ?? "").trim();
  const directive = String(body.directive ?? "").trim();
  if (!projectId || !directive) {
    return NextResponse.json({ error: "project_id and directive are required" }, { status: 400 });
  }
  try {
    const r = await fetch(`${API_URL}/v1/meetings/prd`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      // No invented actor. Claiming a principal you are not is impersonation even when it is
      // an accident — the API rightly refuses it ("'ceo' is not allowed to act as 'operator'").
      // Omitted, the action is attributed to whoever is actually authenticated.
      body: JSON.stringify({
        project_id: projectId,
        directive,
        ...(String(body.actor ?? "").trim() ? { actor: String(body.actor).trim() } : {}),
      }),
      cache: "no-store",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      return NextResponse.json({ error: data?.detail ?? `HTTP ${r.status}` }, { status: r.status });
    }
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
