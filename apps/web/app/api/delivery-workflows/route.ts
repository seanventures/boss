import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";

/**
 * Everything the delivery-workflow screen reads and does, in one place.
 *
 * A route handler rather than Server Actions: this screen refreshes while a workflow is
 * running, and a Server Action reply re-renders the whole console route — which on v1 costs
 * seconds. `view` and `action` select the upstream call so the screen needs one client module
 * rather than nine.
 */
export const dynamic = "force-dynamic";

const VIEWS: Record<string, (project: string) => string> = {
  catalogue: () => "/v1/delivery-workflows/catalogue",
  capabilities: () => "/v1/delivery-workflows/capabilities",
  installed: (p) => `/v1/delivery-workflows?project_id=${encodeURIComponent(p)}`,
  runs: (p) => `/v1/delivery-workflows/runs?project_id=${encodeURIComponent(p)}`,
};

export async function GET(req: Request) {
  const params = new URL(req.url).searchParams;
  const view = params.get("view") ?? "runs";
  const project = params.get("project_id")?.trim() ?? "";
  const build = VIEWS[view];
  if (!build) {
    return NextResponse.json(
      { error: `unknown view '${view}'. Known: ${Object.keys(VIEWS).join(", ")}` },
      { status: 400 },
    );
  }
  if (!project && view !== "catalogue" && view !== "capabilities") {
    return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  }
  try {
    const r = await fetch(`${API_URL}${build(project)}`, {
      headers: authHeaders({}),
      cache: "no-store",
    });
    if (!r.ok) {
      return NextResponse.json({ error: `API returned ${r.status}` }, { status: r.status });
    }
    return NextResponse.json(await r.json());
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}

const ACTIONS: Record<string, string> = {
  install: "/v1/delivery-workflows/catalogue/install",
  apply: "/v1/delivery-workflows/apply",
  fork: "/v1/delivery-workflows/fork",
  save: "/v1/delivery-workflows/save",
  start: "/v1/delivery-workflows/start",
  advance: "/v1/delivery-workflows/run-capabilities",
};

export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = (await req.json()) as Record<string, unknown>;
  } catch {
    return NextResponse.json({ error: "body must be JSON" }, { status: 400 });
  }
  const action = String(body.action ?? "");
  const path = ACTIONS[action];
  if (!path) {
    return NextResponse.json(
      { error: `unknown action '${action}'. Known: ${Object.keys(ACTIONS).join(", ")}` },
      { status: 400 },
    );
  }
  const { action: _drop, ...payload } = body;
  try {
    const r = await fetch(`${API_URL}${path}`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
      cache: "no-store",
    });
    const raw = await r.text();
    let parsed: unknown;
    try { parsed = JSON.parse(raw); } catch { parsed = { detail: raw }; }
    if (!r.ok) {
      // The API's own message, forwarded verbatim. These are the sentences the builder shows
      // beside a step — "step 'd' is risk tier 2 and always requires a person" is the whole
      // point, and replacing it with "request failed" would waste it.
      const detail = (parsed as { detail?: string })?.detail;
      return NextResponse.json({ error: detail || `API returned ${r.status}` },
                               { status: r.status });
    }
    return NextResponse.json(parsed);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
