import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite } from "../../../lib/api-write";
import { apiErrorText } from "../../../lib/api-error";

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
  // BUILD STARTS THE WORKFLOW, NOT THE PANEL.
  //
  // This posted straight to /v1/meetings/prd: a panel convened, a PRD was drafted, somebody
  // approved it, and only THEN — inside start_code_task — did a workflow run appear. So the
  // workflow was a record written after the fact, adopting a meeting and an approval that had
  // already happened, which is why its first three steps completed in 20s, 0s and "not run",
  // and why nothing showed on the Workflow page for the first several minutes.
  //
  // The outer loop is supposed to DRIVE. Starting the run first means its own `meeting` step
  // convenes the panel (the same convene_prd_meeting_core), `spec_review` waits on the PRD
  // approval that panel raises, and `build` creates the project. One press, and every step
  // after it is visible from the first second.
  const title = directive.length > 72 ? `${directive.slice(0, 69)}…` : directive;
  try {
    const r = await apiWrite(`${API_URL}/v1/delivery-workflows/start`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      // No invented actor. Claiming a principal you are not is impersonation even when it is
      // an accident — the API rightly refuses it ("'ceo' is not allowed to act as 'operator'").
      // Omitted, the action is attributed to whoever is actually authenticated.
      body: JSON.stringify({
        project_id: projectId,
        work_class: "project.new",
        input: { title, directive },
        ...(String(body.actor ?? "").trim() ? { actor: String(body.actor).trim() } : {}),
      }),
      cache: "no-store",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      return NextResponse.json({ error: apiErrorText(data, r.status) }, { status: r.status });
    }
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
