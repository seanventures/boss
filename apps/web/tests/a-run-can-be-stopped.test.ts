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
    expect(ENGINE).toMatch(/if active:\n\s+out\.append\(_verdict\("stop", "Stop this run", ENABLED\)/);
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

  it("stops BEFORE it starts", () => {
    const at = UI.indexOf("onStopAndRerun={async (run)");
    expect(at).toBeGreaterThan(-1);
    const body = UI.slice(at, at + 700);
    expect(body.indexOf('"stop"')).toBeLessThan(body.indexOf('"start"'));
  });

  it("and does not start at all if the stop failed", () => {
    // Otherwise the one button whose job is to leave a single run running leaves two.
    expect(UI).toContain("if (!stopped?.ok) return;");
  });

  it("the step-level retry is still offered as the cheaper option", () => {
    // Restarting throws away a panel and a research step that may have been fine.
    expect(UI).toContain("that keeps everything already done");
  });
});
