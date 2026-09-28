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

describe("removing a run", () => {
  it("is offered on a run that is over", () => {
    expect(UI).toContain('data-testid="dw-delete-run"');
    expect(UI).toContain("Remove this run");
  });

  it("is NOT offered while the run is still going", () => {
    // The control lives in the `else` of the active check, beside "Run this workflow again".
    const active = UI.indexOf("ACTIVE_RUN_STATES.has(run.status) ? (");
    const rerun = UI.indexOf('data-testid="dw-rerun"');
    const remove = UI.indexOf('data-testid="dw-delete-run"');
    expect(active).toBeGreaterThan(-1);
    expect(remove).toBeGreaterThan(rerun);
    expect(rerun).toBeGreaterThan(active);
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
