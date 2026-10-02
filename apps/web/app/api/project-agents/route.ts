import { NextResponse } from "next/server";

import { writeAwareFetch } from "../../../lib/api-write";
import { API_URL, authHeaders } from "../../../lib/client";
import { apiErrorText } from "../../../lib/api-error";

/**
 * Who is on an app.
 *
 * BOSS had no answer to that: `roles` says an agent exists in a project, a workflow step names
 * an `approver_role`, and nothing anywhere records who is on a given piece of work.
 */
export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const params = new URL(req.url).searchParams;
  const projectId = params.get("project_id")?.trim();
  const app = params.get("app")?.trim() ?? "";
  // WHO WOULD BE ASSIGNED, for something that does not exist yet. The new-project wizard needs
  // that before there is an app to ask about, and a guess of its own would drift from what
  // start_run actually seeds.
  const defaults = params.get("defaults")?.trim() === "1";
  const workflowKey = params.get("workflow_key")?.trim() ?? "";
  if (!projectId) return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  const query = defaults
    ? `?defaults=1${workflowKey ? `&workflow_key=${encodeURIComponent(workflowKey)}` : ""}`
    : (app ? `?app=${encodeURIComponent(app)}` : "");
  return relay(
    `${API_URL}/v1/projects/${encodeURIComponent(projectId)}/agents${query}`,
    { headers: authHeaders({}) });
}

export async function POST(req: Request) {
  const body = (await req.json().catch(() => ({}))) as Record<string, unknown>;
  const projectId = String(body.project_id ?? "").trim();
  if (!projectId) return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  return relay(`${API_URL}/v1/projects/${encodeURIComponent(projectId)}/agents`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
}

async function relay(url: string, init: RequestInit) {
  try {
    const r = await writeAwareFetch(url, { ...init, cache: "no-store" });
    const raw = await r.text();
    let parsed: unknown;
    try { parsed = JSON.parse(raw); } catch { parsed = { detail: raw }; }
    if (!r.ok) {
      // The API's own sentence: a refusal here names which agents ARE assigned, and replacing
      // that with "request failed" would throw away the only actionable part.
      return NextResponse.json(
        { error: apiErrorText(parsed, r.status) },
        { status: r.status });
    }
    return NextResponse.json(parsed);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
