/**
 * The deploy step shows where it ships.
 *
 * The operator asked for this control roughly ten times and was told three times it was done.
 * It never rendered once, because every version tested `step.capability === "deploy"` — and the
 * runs endpoint returns `capability: null` on step rows. The condition could not be true.
 *
 *     step_id   : "deploy"
 *     capability: None        <- what the condition tested
 *
 * The other two versions added further gates (only inside the approval block; only while
 * !succeeded && !needsYou), each of which independently hid it on the state the operator's run
 * was actually in. Three fixes, none of which touched the reason.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const UI = readFileSync(join(process.cwd(), "app/delivery-workflows-ui.tsx"), "utf8");

/** The predicate, reimplemented exactly as shipped, so the data shape is the thing under test. */
type Step = { step_id: string; capability?: string | null; status: string };
const isDeployStep = (s: Step, capKey?: string) =>
  s.step_id === "deploy" || s.capability === "deploy" || capKey === "deploy";

describe("recognising the deploy step", () => {
  it("recognises the shape the API actually returns", () => {
    // Verbatim from GET /v1/delivery-workflows/runs — capability is null on the wire.
    const fromApi: Step = { step_id: "deploy", capability: null, status: "succeeded" };
    expect(isDeployStep(fromApi)).toBe(true);
  });

  it("would have been false under the old condition", () => {
    const fromApi: Step = { step_id: "deploy", capability: null, status: "succeeded" };
    expect(fromApi.capability === "deploy").toBe(false);   // the bug, stated
  });

  it.each(["succeeded", "failed", "waiting", "pending", "leased", "skipped"])(
    "recognises it in the %s state", (status) => {
      expect(isDeployStep({ step_id: "deploy", capability: null, status })).toBe(true);
    });

  it("still recognises a renamed step by its capability", () => {
    expect(isDeployStep({ step_id: "ship_it", capability: "deploy", status: "waiting" })).toBe(true);
    expect(isDeployStep({ step_id: "ship_it", capability: null, status: "waiting" }, "deploy")).toBe(true);
  });

  it("does not claim other steps", () => {
    for (const id of ["build", "preview", "monitor", "notify", "research"]) {
      expect(isDeployStep({ step_id: id, capability: null, status: "waiting" })).toBe(false);
    }
  });
});

describe("the picker is not gated on anything else", () => {
  it("renders on a deploy step whatever its status", () => {
    // Two layouts now — a numbered plan while no target is bound, one action once it is — and
    // NEITHER is gated on the step's status. That was the bug this file was written for: each
    // version of the condition excluded the state the operator's run happened to be in.
    expect(UI).toContain(") : isDeployStep(step, capability) && shippedTo ? (");
    expect(UI).toContain(") : isDeployStep(step, capability) ? (");
    const at = UI.indexOf(") : isDeployStep(step, capability) && shippedTo ? (");
    const block = UI.slice(at, at + 300);
    expect(block).not.toContain('"succeeded"');
  });

  it("appears exactly once, so the gate does not show two pickers", () => {
    // The guard that caught the layout change honestly: the control is CREATED once and placed
    // by whichever layout is active. Two call sites would be one edit away from rendering both,
    // which is the reported "he may click set target multiple times" waiting to happen again —
    // and is precisely what one of the reference designs did on purpose.
    expect(UI.split("<DeployWhere").length - 1).toBe(1);
    expect(UI).toContain("const targetControl = (");
    expect(UI.split("{targetControl}").length - 1).toBe(2);
  });
});
