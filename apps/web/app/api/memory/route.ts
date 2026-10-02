import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite } from "../../../lib/api-write";
import { apiErrorText } from "../../../lib/api-error";

/** What this project knows, and the rules waiting to be allowed to bind.
 *
 *  One read serves both the browse view and the review queue: a proposed lesson comes back
 *  flagged rather than living behind a second endpoint nobody opens.
 */
export async function GET(req: Request) {
  const params = new URL(req.url).searchParams;
  const project = params.get("project_id")?.trim() ?? "";
  const actor = params.get("actor")?.trim() ?? "";
  if (!project) {
    return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  }
  const qs = new URLSearchParams({ project_id: project, include_proposed: "true" });
  if (actor) qs.set("actor", actor);
  try {
    const r = await fetch(`${API_URL}/v1/memory?${qs}`, {
      headers: authHeaders({}), cache: "no-store",
    });
    if (!r.ok) {
      return NextResponse.json({ error: `API returned ${r.status}` }, { status: r.status });
    }
    return NextResponse.json(await r.json());
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}

/** Review a proposed rule, or retire one that is wrong.
 *
 *  `action` is `review` (with a decision) or `forget`. Both are writes that change what every
 *  future session is told, so both go through the API's project-actor check rather than being
 *  trusted from the browser.
 */
export async function POST(req: Request) {
  let body: {
    action?: string; memory_id?: string; project_id?: string; actor?: string;
    decision?: string;
  };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON" }, { status: 400 });
  }
  const id = (body?.memory_id ?? "").trim();
  const project_id = (body?.project_id ?? "").trim();
  const actor = (body?.actor ?? "").trim();
  const action = (body?.action ?? "").trim();
  if (!id || !project_id || !actor) {
    return NextResponse.json(
      { error: "memory_id, project_id and actor are required" }, { status: 400 });
  }
  if (action !== "review" && action !== "forget") {
    return NextResponse.json({ error: "action must be 'review' or 'forget'" }, { status: 400 });
  }
  try {
    const r = await apiWrite(
      `${API_URL}/v1/memory/${encodeURIComponent(id)}/${action}`,
      {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ project_id, actor, decision: body?.decision }),
        cache: "no-store",
      },
    );
    const payload = await r.json().catch(() => ({}));
    if (!r.ok) {
      return NextResponse.json(
        { error: apiErrorText(payload, r.status) }, { status: r.status });
    }
    return NextResponse.json(payload);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
