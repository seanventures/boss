import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * "There are 3 workflows running for same project — we should expose option to stop the
 * workflow and Run this workflow again does not stop the current run and start from the
 * beginning."
 *
 * The screen already knew. Its disabled Re-run button explained: "Starting it again would make
 * a second workflow for the same project… or cancel this run first" — advice naming an action
 * the product did not offer anywhere. So the only way out of a run you no longer wanted was to
 * start another one, which is how three ended up going at once.
 */
const UI = codeOnly("app/delivery-workflows-ui.tsx");
const ROUTE = codeOnly("app/api/delivery-workflows/route.ts");
// The label and WHEN it is offered are the engine's (workflow_run_actions.py) since a
// cancelled run whose research failed was offered "Build on this research" by a console rule.
const ENGINE = readFileSync("../../engine/boss_engine/services/workflow_run_actions.py", "utf8");

describe("stopping a run", () => {
  it("is a button on a live run", () => {
    expect(UI).toContain('testId: "dw-stop"');
    // REWRITTEN to the verdict form; cancel_run now refuses through the same verdict.
    // Live means every status the engine's cancel_run will stop — `blocked` included.
    // REWRITTEN (review of c8de36b): and a run STOPPED at a failed step — waiting for the
    // operator, not over — so it can be abandoned, and a branch still working under it stopped.
    expect(ENGINE).toMatch(
      /if active or stopped:\n\s+out\.append\(_verdict\("stop", "Stop this run", ENABLED\)/);
  });

  it("reaches the cancel endpoint, addressed to the run", () => {
    expect(ROUTE).toContain("/v1/workflow-runs/${encodeURIComponent(id)}/cancel");
    expect(ROUTE).toContain("stop:");
  });

  it("and says it stops the WORK, not the bookkeeping", () => {
    // The distinction that once cost a production database: a cancelled run whose build kept
    // going, into a QA step, against the DATABASE_URL it had inherited.
    expect(UI).toContain("Stops the work, not just the record");
    // Phrases wrap across JSX lines, so assert on fragments that do not straddle a break.
    expect(UI).toContain("anything this run started");
    expect(UI).toContain("a build that is still going");
  });

  it("the dead button is gone", () => {
    expect(UI).not.toContain('data-testid="dw-rerun-blocked"');
  });
});

describe("stop and run again", () => {
  it("is one press", () => {
    expect(UI).toContain('testId: "dw-stop-and-rerun"');
    expect(ENGINE).toContain('_verdict("stop_and_rerun", "Stop and run again", ENABLED)');
  });

  // REWRITTEN (review of the readiness change). These asserted the browser sent a stop and then
  // a start. Once Start could refuse a project that lacks something, that order destroyed the
  // operator's run: the stop landed, the start was refused, and nothing was left running. The
  // press is now ONE start naming `replaces_run`, and the SERVER checks, then stops, then starts
  // in one transaction (apps/api/tests/test_a_template_that_cannot_run_here_is_refused_at_start
  // proves the original survives a refusal). Stop still lands before start — on the server.
  it("is one request that names the run it replaces, never a stop then a start", () => {
    const at = UI.indexOf("onStopAndRerun={async (run)");
    expect(at).toBeGreaterThan(-1);
    const body = UI.slice(at, UI.indexOf("onBuildOnThis", at));
    expect(body).toContain("replaces_run: run.id");
    expect(body).not.toContain('"stop"');
    expect(body.match(/act\(/g)?.length).toBe(1);
  });

  it("and the server refuses before it stops anything", () => {
    const API = readFileSync("../api/app/main.py", "utf8");
    const at = API.indexOf("def start_delivery_workflow(");
    const body = API.slice(at, API.indexOf("\ndef _delivery_readiness_or_409(", at));
    const check = body.indexOf("readiness = _delivery_readiness_or_409(");
    const stop = body.indexOf("workflow_runtime.cancel_run(");
    const start = body.indexOf("workflow_runtime.start_run(");
    expect(check).toBeGreaterThan(-1);
    expect(check).toBeLessThan(stop);
    expect(stop).toBeLessThan(start);
  });

  it("the step-level retry is still offered as the cheaper option", () => {
    // Restarting throws away a panel and a research step that may have been fine.
    expect(UI).toContain("that keeps everything already done");
  });
});
