import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

/**
 * There was no way to remove a run from this screen.
 *
 * "Stop this run" leaves the row, and a run that failed is already finished — so stopping one
 * did nothing at all. Building one workflow left eighteen runs on the screen and no way to
 * clear any of them: "lots of workflows shows error and waiting.. please clean up the mess".
 */
const UI = readFileSync(new URL("../app/delivery-workflows-ui.tsx", import.meta.url), "utf8");
const ROUTE = readFileSync(
  new URL("../app/api/delivery-workflows/route.ts", import.meta.url), "utf8");
// WHEN it is offered is the engine's call (workflow_run_actions.py), held to `delete_run`'s own
// rule. The console's copy of it offered Remove on a `blocked` run, which delete_run refuses.
const ENGINE = readFileSync(
  new URL("../../../engine/boss_engine/services/workflow_run_actions.py", import.meta.url), "utf8");

describe("removing a run", () => {
  it("is offered on a run that is over", () => {
    expect(UI).toContain('testId: "dw-delete-run"');
    expect(ENGINE).toContain('_verdict("remove", "Remove this run", ENABLED)');
  });

  it("is NOT offered while the run is still going", () => {
    // Decided by the engine beside "Run this workflow again", on a FINISHED run only — the
    // states `delete_run` accepts. A test there holds FINISHED_RUN_STATES equal to them.
    // REWRITTEN to the verdict form; delete_run now refuses through the same verdict.
    // REWRITTEN AGAIN (review of c8de36b): a run STOPPED at a failed step is the operator's,
    // not over — Remove is offered on it only once nothing under it is live (it used to delete
    // the records while a parallel branch went on). So Remove is enabled outright on a CLOSED
    // run, and on a stopped one only when no step is live; engine/tests/
    // test_a_stopped_run_is_the_operators.py enforces the behaviour.
    const closed = ENGINE.indexOf("    if closed:\n", ENGINE.indexOf("    live = ["));
    const rerun = ENGINE.indexOf('_verdict("rerun"', closed);
    const remove = ENGINE.indexOf('_verdict("remove", "Remove this run", ENABLED)', closed);
    expect(closed).toBeGreaterThan(-1);
    expect(rerun).toBeGreaterThan(closed);
    expect(remove).toBeGreaterThan(rerun);
    expect(remove - closed).toBeLessThan(200);
    const stopped = ENGINE.indexOf("    elif stopped:\n", remove);
    expect(stopped).toBeGreaterThan(remove);
    expect(ENGINE.slice(stopped, stopped + 1400)).toContain('"remove", "Remove this run", DISABLED');
  });

  it("asks first, because it cannot be undone", () => {
    expect(UI).toContain("window.confirm(");
    expect(UI).toContain("This cannot be undone.");
  });

  it("says what it does NOT touch, so nobody thinks it deletes their video", () => {
    expect(UI).toContain("anything published — is untouched");
  });

  it("goes over DELETE rather than through the action switch", () => {
    // The route's own rule: an action name is a string, and a typo in one must not be able to
    // destroy something.
    expect(UI).toContain('{ method: "DELETE" }');
    expect(ROUTE).toContain('const runId = params.get("run_id")?.trim() ?? "";');
    expect(ROUTE).toContain("/v1/delivery-workflows/runs/");
    expect(ROUTE).not.toContain("delete_run:");
  });

  it("closes the open run and reloads, so the screen matches what is left", () => {
    expect(UI).toContain("setOpenRun(null); await load();");
  });
});
