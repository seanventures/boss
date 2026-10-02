import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite } from "../../../lib/api-write";

/**
 * The end-of-run report: what each step cost, who did it, and what went wrong.
 *
 * Proxied rather than fetched from the browser for the same reason as every other route here:
 * `/v1/` is auth-gated and tailnet-only, so a direct call returns a 401 the page would have to
 * render as a report.
 */
export const dynamic = "force-dynamic";

function ids(req: Request) {
  const params = new URL(req.url).searchParams;
  return {
    runId: (params.get("run_id") ?? "").trim(),
    projectId: (params.get("project_id") ?? "").trim(),
  };
}

export async function GET(req: Request) {
  const { runId, projectId } = ids(req);
  if (!runId) return NextResponse.json({ ok: false, error: "missing run_id" }, { status: 400 });
  try {
    const r = await fetch(
      `${API_URL}/v1/workflow-runs/${encodeURIComponent(runId)}/report`
        + `?project_id=${encodeURIComponent(projectId)}`,
      { headers: authHeaders({ "Content-Type": "application/json" }), cache: "no-store" },
    );
    return NextResponse.json(await r.json(), { status: r.ok ? 200 : r.status });
  } catch {
    return NextResponse.json({ ok: false, error: "the report could not be read" });
  }
}

/** Keep it as a document, so it can be downloaded. Returns the artifact id to link to. */
export async function POST(req: Request) {
  const { runId, projectId } = ids(req);
  if (!runId) return NextResponse.json({ ok: false, error: "missing run_id" }, { status: 400 });
  try {
    const r = await apiWrite(
      `${API_URL}/v1/workflow-runs/${encodeURIComponent(runId)}/report`
        + `?project_id=${encodeURIComponent(projectId)}`,
      { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
        cache: "no-store" },
    );
    return NextResponse.json(await r.json(), { status: r.ok ? 200 : r.status });
  } catch {
    return NextResponse.json({ ok: false, error: "the report could not be saved" });
  }
}
