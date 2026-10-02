import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite } from "../../../lib/api-write";

/**
 * Start a Discovery run without re-rendering the console.
 *
 * This went through startDiscoveryAction, a Server Action that also calls revalidatePath("/").
 * A Server Action already re-renders the current route to return a fresh RSC payload, and
 * revalidatePath forces it — so "start a research run" waited on a 20-27s console render
 * before the operator saw anything. Observed live at 163 seconds, still spinning, for a call
 * that takes about a second to enqueue.
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
  const seed = String(body.seed ?? "").trim();
  if (!projectId || !seed) {
    return NextResponse.json({ error: "project_id and seed are required" }, { status: 400 });
  }
  try {
    const r = await apiWrite(`${API_URL}/v1/discoveries`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ project_id: projectId, seed, title: body.title, context: body.context }),
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
