import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite } from "../../../lib/api-write";
import { loadPath, WRITING_GET_TWIN_SUFFIX } from "../../../lib/writing-get";

/**
 * The run surfaces' reads of signed work: a run's chain, and one piece of work in detail.
 *
 *   ?view=chain&run_id=wfr_…&project_id=…  → GET /v1/workflow-runs/{id}/chain
 *   ?view=act&act_id=act_…&project_id=…    → GET /v1/acts/{act_id}
 *
 * Proxied for the reason every route here is: `/v1/` is auth-gated and tailnet-only, and this
 * route sits behind the console's own session (middleware.ts).
 */
export const dynamic = "force-dynamic";

const ID = /^[A-Za-z0-9_.:-]{1,128}$/;

export async function GET(req: Request) {
  const params = new URL(req.url).searchParams;
  const view = params.get("view") ?? "";
  const projectId = (params.get("project_id") ?? "").trim();
  let path: string;
  if (view === "chain") {
    const runId = (params.get("run_id") ?? "").trim();
    if (!ID.test(runId)) return NextResponse.json({ error: "missing run_id" }, { status: 400 });
    // A build (crun_…) has its own chain: its acts, in the workflow chain shape (GET /v1/code-tasks/{id}/chain).
    // A workflow run's chain is a writing GET (it freezes the chain): read by its POST twin.
    path = runId.startsWith("crun_")
      ? `/v1/code-tasks/${encodeURIComponent(runId)}/chain`
      : loadPath(`/v1/workflow-runs/${encodeURIComponent(runId)}/chain`);
  } else if (view === "act") {
    const actId = (params.get("act_id") ?? "").trim();
    if (!ID.test(actId)) return NextResponse.json({ error: "missing act_id" }, { status: 400 });
    path = `/v1/acts/${encodeURIComponent(actId)}`;
  } else {
    return NextResponse.json({ error: "unknown view" }, { status: 400 });
  }
  try {
    const url = `${API_URL}${path}?project_id=${encodeURIComponent(projectId)}`;
    const headers = authHeaders({ "Content-Type": "application/json" });
    const r = path.endsWith(WRITING_GET_TWIN_SUFFIX)
      ? await apiWrite(url, { method: "POST", headers, cache: "no-store" })
      : await fetch(url, { headers, cache: "no-store" });
    return NextResponse.json(await r.json(), { status: r.ok ? 200 : r.status });
  } catch {
    return NextResponse.json({ error: "the work record could not be read" }, { status: 502 });
  }
}
