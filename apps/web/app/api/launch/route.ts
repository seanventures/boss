import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";

/**
 * Start a project from a brief — the reference's launch modal.
 *
 * Goes to /v1/meetings/prd, which is the documented entry point for "build X" and actually
 * starts the sequence: panel -> PRD -> your approval -> code task.
 *
 * NOT the older "assign an outcome" path. That created a task, ran one planning job, wrote a
 * plan artifact and STOPPED — an operator clicked, watched "Working", and nothing else ever
 * happened, because nothing downstream consumes a plan. The task really was created and really
 * did sit at `open` forever.
 *
 * /v1/meetings/prd dedupes on (project, directive), so a double-click is one meeting rather
 * than two panels debating the same thing.
 */
export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  const body = (await req.json().catch(() => ({}))) as Record<string, unknown>;
  const directive = String(body.directive ?? "").trim();
  const projectId = String(body.project_id ?? "").trim();
  const actor = String(body.actor ?? "").trim();

  if (!projectId) {
    return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  }
  if (!directive) {
    return NextResponse.json({ error: "describe what to build first" }, { status: 400 });
  }
  if (!actor) {
    return NextResponse.json({ error: "actor is required" }, { status: 400 });
  }

  try {
    const r = await fetch(`${API_URL}/v1/meetings/prd`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ project_id: projectId, actor, directive }),
      cache: "no-store",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      return NextResponse.json(
        { error: (data as { detail?: string })?.detail ?? `HTTP ${r.status}` },
        { status: r.status },
      );
    }
    // The ids come back so the caller can link to what it just started. Returning nothing is
    // why an earlier version could not confirm, link, or report a failure it had.
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
