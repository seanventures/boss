/**
 * The plan lists what it will run, in the shape the ENGINE sends (steps grouped by phase).
 * The console read a flat list, its test fake sent a flat list, and every real plan on boss-1
 * said "No steps recorded" (2026-10-03).
 */
import { describe, expect, it } from "vitest";
import { planSteps } from "../app/stack-deploy-ui";

describe("a stack plan shows the steps the engine sends", () => {
  it("reads the engine's phased summary", () => {
    // DeployManifest.summary() for kind: compose, as GET /v1/deployments/stack/{id} returns it.
    expect(planSteps({ install: ["compose up"], update: ["compose up"], teardown: ["compose down"] }))
      .toEqual(["install: compose up", "update: compose up", "teardown: compose down"]);
  });

  it("still reads a flat list and nothing at all", () => {
    expect(planSteps(["a", { name: "b", command: "run b" }])).toEqual(["a", "b: run b"]);
    expect(planSteps(undefined)).toEqual([]);
  });
});
