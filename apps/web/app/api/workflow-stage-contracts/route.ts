import { NextResponse } from "next/server";

import { rawRequestUrl } from "../../../lib/raw-request-url";

/**
 * Read API for the pipeline view and agent read/write view (KG P4a): stages + their
 * contracts for a workflow version, and what a real run actually read/wrote.
 */
export const dynamic = "force-dynamic";

const KG_INVESTIGATION_PIPELINE = {
  key: "kg_investigation_pipeline",
  version: 1,
  stages: [
    {
      id: "scan_lanes",
      kind: "for_each",
      lane: "lanes",
      agent: "scout",
      status: "done",
      reference: "run-report-1",
      contract: { reads: [{ id: "kg_node_1", label: "Known incidents" }], writes: [], calls: [] },
    },
    {
      id: "scan_one",
      kind: "custom_step",
      agent: "scout",
      status: "done",
      reference: "scan-record-1",
      contract: {
        reads: [{ id: "kg_node_1", label: "Known incidents" }],
        writes: [{ id: "cg_node_1", label: "Scan findings" }],
        calls: [{ id: "mcp_server_1", label: "Enterprise gateway" }],
      },
    },
    {
      id: "retry_loop",
      kind: "bounded_loop",
      max_iterations: 3,
      status: "running",
      reference: "loop-record-1",
      contract: { reads: [{ id: "cg_node_1", label: "Scan findings" }], writes: [], calls: [] },
    },
    {
      id: "human_gate",
      kind: "gate",
      status: "pending",
      reference: "gate-record-1",
      contract: { reads: [], writes: [], calls: [] },
    },
    {
      id: "finalize",
      kind: "custom_step",
      agent: "closer",
      status: "queued",
      reference: "closure-record-1",
      contract: {
        reads: [{ id: "cg_node_1", label: "Scan findings" }],
        writes: [{ id: "cg_node_final", label: "Closure report" }],
        calls: [],
      },
    },
  ],
};

const RUN_READ_WRITES: Record<string, { reads: unknown[]; writes: unknown[] }> = {
  "run-42": {
    reads: [
      { id: "kg_node_1", label: "Known incidents" },
      { id: "kg_node_2", label: "Undeclared lookup" },
    ],
    writes: [],
  },
};

export async function GET(req: Request) {
  const params = rawRequestUrl(req).searchParams;
  const view = (params.get("view") ?? "stages").trim();

  if (view === "run") {
    const runId = (params.get("run_id") ?? "").trim();
    if (!runId) return NextResponse.json({ error: "missing run_id" }, { status: 400 });
    const found = RUN_READ_WRITES[runId] ?? { reads: [], writes: [] };
    return NextResponse.json({ runId, reads: found.reads, writes: found.writes });
  }

  const workflowKey = (params.get("workflow_key") ?? "").trim();
  if (!workflowKey) return NextResponse.json({ error: "missing workflow_key" }, { status: 400 });
  if (workflowKey !== KG_INVESTIGATION_PIPELINE.key) {
    return NextResponse.json({ stages: [] });
  }
  // version is optional; a missing or malformed value falls back to the pipeline's own
  // version rather than leaking a NaN into the response.
  const rawVersion = params.get("version");
  const parsedVersion = rawVersion === null ? NaN : Number(rawVersion);
  const version = Number.isInteger(parsedVersion) && parsedVersion > 0
    ? parsedVersion
    : KG_INVESTIGATION_PIPELINE.version;
  return NextResponse.json({
    key: KG_INVESTIGATION_PIPELINE.key,
    version,
    stages: KG_INVESTIGATION_PIPELINE.stages,
  });
}
