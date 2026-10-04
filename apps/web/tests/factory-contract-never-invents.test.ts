import { describe, expect, it } from "vitest";

import { parseCockpit } from "../lib/cockpit";
import { parseBuildRow, parseFloor, parseStation } from "../lib/factory-floor";
import { ROWS, cockpit, cockpitChecksFailed, floor } from "./_helpers/factory-fixtures";

describe("the Factory contract never invents a field", () => {
  it("parses every state fixture with k never past n", () => {
    for (const [name, row] of Object.entries(ROWS)) {
      const b = parseBuildRow(row);
      expect(b.k, name).toBeLessThanOrEqual(b.n);
      expect(b.stations.length, name).toBeGreaterThan(0);
    }
  });

  it("an agent face without a role is never drawn", () => {
    expect(parseStation({ key: "build", face: "agent", status: "working" }).face).toBe("none");
    expect(parseStation({ key: "review", face: "hollow", status: "pending" }).face).toBe("none");
    expect(parseStation({ key: "pr", face: "boss", status: "pending" }).face).toBe("boss");
  });

  it("unmetered cost is 0, never shown", () => {
    expect(parseBuildRow({ cost_usd: 1.5, metered: false }).costUsd).toBe(0);
  });

  it("parses the floor and the build attention", () => {
    const f = parseFloor(floor());
    const gates = f.attention.find((a) => a.kind === "gate_override");
    expect(gates?.build?.failing.find((g) => g.name === "app_serves")?.waivable).toBe(false);
    expect(f.builds[0].stations.some((s) => s.key === "you_plan")).toBe(true);
  });

  it("parses the cockpit", () => {
    const c = parseCockpit(cockpit());
    expect(c.story.map((r) => r.type)).toEqual(["boss", "act", "act", "you", "act"]);
    expect(c.now?.arg).toBe("src/invoices/export.ts");
    const cf = parseCockpit(cockpitChecksFailed());
    expect(cf.outputs.checks?.gates.filter((g) => !g.ok)).toHaveLength(2);
  });
});
