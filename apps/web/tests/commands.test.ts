import { describe, expect, it } from "vitest";

import { buildCommandRequest } from "../lib/commands";

describe("buildCommandRequest", () => {
  it("trims command input and includes deadline when present", () => {
    const payload = buildCommandRequest(
      "  Ship the next slice.  ",
      "  this week ",
    );

    expect(payload.command).toBe("Ship the next slice.");
    expect(payload.constraints).toEqual({ deadline: "this week" });
  });

  it("throws when the command is empty", () => {
    expect(() => buildCommandRequest("   ", null)).toThrow("command is required");
  });
});
