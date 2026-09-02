import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";

/** Route handler rather than a server action: the console takes seconds to re-render, and a
 *  drag whose effect appears only after a full re-render reads as a drag that did nothing. */
export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const projectId = new URL(req.url).searchParams.get("project_id")?.trim();
  if (!projectId) return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  try {
    const r = await fetch(
      `${API_URL}/v1/console/v2/org?project_id=${encodeURIComponent(projectId)}`,
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
  const agentId = String(body.agent_id ?? "").trim();
  if (!agentId) return NextResponse.json({ error: "agent_id is required" }, { status: 400 });
  try {
    const r = await fetch(
      `${API_URL}/v1/console/v2/org/${encodeURIComponent(agentId)}`, {
        method: "PUT",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(body),
        cache: "no-store",
      });
    const parsed = await r.json().catch(() => ({}));
    // The API's own sentence. "X already sits above Y, so this would make a loop" tells the
    // operator what to do differently; "could not save" sends them looking in the wrong place.
    if (!r.ok) {
      return NextResponse.json(
        { error: (parsed as { detail?: string })?.detail ?? `API returned ${r.status}` },
        { status: r.status });
    }
    return NextResponse.json(parsed);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}

export async function DELETE(req: Request) {
  const params = new URL(req.url).searchParams;
  const projectId = params.get("project_id")?.trim();
  const agentId = params.get("agent_id")?.trim();
  if (!projectId || !agentId) {
    return NextResponse.json({ error: "project_id and agent_id are required" }, { status: 400 });
  }
  try {
    const r = await fetch(
      `${API_URL}/v1/console/v2/org/${encodeURIComponent(agentId)}`
      + `?project_id=${encodeURIComponent(projectId)}`,
      { method: "DELETE", headers: authHeaders(), cache: "no-store" });
    const parsed = await r.json().catch(() => ({}));
    return NextResponse.json(parsed, { status: r.ok ? 200 : r.status });
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
