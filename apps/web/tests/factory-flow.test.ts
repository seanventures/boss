import { describe, expect, it } from "vitest";

import { mapFactoryFlowBoard } from "../lib/factory-flow";

describe("factory flow mapper", () => {
  it("does not invent stalled runs when the payload is empty", () => {
    const board = mapFactoryFlowBoard({ project_id: "boss-dogfood", items: [], findings: [] }, "boss-dogfood");
    expect(board.items).toEqual([]);
    expect(board.findings).toEqual([]);
    expect(board.stalled).toBe(0);
    expect(board.apiReachable).toBe(true);
  });

  it("keeps diagnosis text from the API", () => {
    const board = mapFactoryFlowBoard(
      {
        project_id: "boss-dogfood",
        stalled: 1,
        in_flight: 1,
        items: [
          {
            id: "crun_1",
            title: "HookWatch",
            status: "failed",
            phase: "done",
            diagnosis: {
              klass: "factory_crash",
              state: "failed",
              title: "Factory crashed",
              recommended_fix: "Keep evidence writes after the dict exists.",
            },
            steps: [{ id: "s1", lane: "factory", name: "review_panel", status: "done" }],
          },
        ],
        findings: [],
      },
      "boss-dogfood",
    );
    expect(board.items[0]?.diagnosis.klass).toBe("factory_crash");
    expect(board.items[0]?.diagnosis.recommendedFix).toContain("evidence");
    expect(board.stalled).toBe(1);
  });
});
