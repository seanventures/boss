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
  it("renders from isDeployStep alone", () => {
    expect(UI).toContain("{isDeployStep(step, capability) ? (");
  });

  it("is not restricted by step status", () => {
    const at = UI.indexOf("{isDeployStep(step, capability) ? (");
    const block = UI.slice(at, at + 400);
    expect(block).not.toContain('"succeeded"');
    expect(block).not.toContain("needsYou");
  });

  it("appears exactly once, so the gate does not show two pickers", () => {
    expect(UI.split("<DeployWhere").length - 1).toBe(1);
  });
});
