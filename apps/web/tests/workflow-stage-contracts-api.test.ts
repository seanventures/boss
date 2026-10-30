// @vitest-environment node
import { describe, it, expect } from "vitest";
import { GET } from "../app/api/workflow-stage-contracts/route";

// The read endpoint P4a adds (only because none existed): for a workflow version it
// returns stages + their contracts, and for a run it returns what that run actually
// read/wrote, reusing the existing view shapes (stage list, contract reads/writes/calls).

function req(url: string) {
  return new Request(`http://localhost${url}`);
}

describe("GET /api/workflow-stage-contracts", () => {
  it("P1 returns stages with contracts for a workflow version, and actual reads/writes for a run", async () => {
    const specRes = await GET(
      req("/api/workflow-stage-contracts?view=stages&workflow_key=kg_investigation_pipeline&version=1")
    );
    expect(specRes.status).toBe(200);
    const specBody = await specRes.json();
    expect(Array.isArray(specBody.stages)).toBe(true);
    expect(specBody.stages.length).toBeGreaterThan(0);
    for (const stage of specBody.stages) {
      expect(stage).toHaveProperty("id");
      expect(stage).toHaveProperty("contract");
      expect(stage.contract).toHaveProperty("reads");
      expect(stage.contract).toHaveProperty("writes");
    }

    const runRes = await GET(req("/api/workflow-stage-contracts?view=run&run_id=run-42"));
    expect(runRes.status).toBe(200);
    const runBody = await runRes.json();
    expect(runBody).toHaveProperty("reads");
    expect(runBody).toHaveProperty("writes");
  });

  it("rejects a stages request with no workflow_key", async () => {
    const res = await GET(req("/api/workflow-stage-contracts?view=stages"));
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body).toHaveProperty("error");
  });

  it("returns no stages for an unknown workflow_key, instead of guessing", async () => {
    const res = await GET(
      req("/api/workflow-stage-contracts?view=stages&workflow_key=not_a_real_pipeline&version=1")
    );
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.stages).toEqual([]);
  });

  it("rejects a run request with no run_id", async () => {
    const res = await GET(req("/api/workflow-stage-contracts?view=run"));
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body).toHaveProperty("error");
  });

  it("returns empty reads/writes for a run_id it has no record of", async () => {
    const res = await GET(req("/api/workflow-stage-contracts?view=run&run_id=run-does-not-exist"));
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.runId).toBe("run-does-not-exist");
    expect(body.reads).toEqual([]);
    expect(body.writes).toEqual([]);
  });

  it("defaults to the stages view and version 1 when not specified", async () => {
    const res = await GET(req("/api/workflow-stage-contracts?workflow_key=kg_investigation_pipeline"));
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.version).toBe(1);
    expect(body.stages.length).toBe(KG_INVESTIGATION_PIPELINE_STAGE_COUNT);
  });

  it("every stage's contract also carries a calls list, for the MCP/tool edges in the agent read/write view", async () => {
    const res = await GET(
      req("/api/workflow-stage-contracts?view=stages&workflow_key=kg_investigation_pipeline&version=1")
    );
    const body = await res.json();
    for (const stage of body.stages) {
      expect(stage.contract).toHaveProperty("calls");
      expect(Array.isArray(stage.contract.calls)).toBe(true);
    }
  });
});

const KG_INVESTIGATION_PIPELINE_STAGE_COUNT = 5;
