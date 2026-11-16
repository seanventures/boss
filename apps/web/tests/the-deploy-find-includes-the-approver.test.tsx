/**
 * Infra Find keeps a deploy when the query names who approved it: the actor or the role key,
 * either case. Someone else does not keep it. An empty query still keeps every row.
 */
import { describe, expect, it } from "vitest";

import { matches } from "../app/infra/deploys";
import type { DeployRow } from "../lib/infra-view";

function row(approver: DeployRow["approver"]): DeployRow {
  return {
    ref: "stack:sd_1", kind: "stack", targetId: null, app: "ledger", env: "staging",
    host: { id: "h1", name: "edge-1" }, account: "acct-1",
    sha7: "abc1234", refName: "main", trigger: "manual", rollbackOfSha7: null,
    approver, state: "succeeded", word: "Live", tone: "green", steps: [],
    startedAt: null, finishedAt: null, durationMs: null,
    can: { approve: false, reject: false, rollback: false, redeploy: false },
    rollbackToSha7: null, decisionId: null, runId: null, failedAfter: null,
    phase: "live", sentence: "Live.", now: null, openUrl: null,
  };
}

const approved = row({ actor: "mina.okonkwo", roleKey: "deployment_manager" });

describe("Infra Find includes the deploy approver", () => {
  it("matches a query of the approver's actor", () => {
    expect(matches(approved, "Mina.Okonkwo")).toBe(true);
  });

  it("matches a query of the approver's role key", () => {
    expect(matches(approved, "Deployment_Manager")).toBe(true);
  });

  it("does not match a different person", () => {
    expect(matches(approved, "sam.chen")).toBe(false);
  });

  it("matches every row when the query is empty", () => {
    expect(matches(approved, "")).toBe(true);
  });
});
