import { describe, expect, it } from "vitest";
import { placeholderStatus } from "../lib/status";

describe("placeholderStatus", () => {
  it("marks API and database as unavailable", () => {
    const status = placeholderStatus("network down");

    expect(status.apiReachable).toBe(false);
    expect(status.databaseConnected).toBe(false);
    expect(status.blockedReason).toBe("network down");
  });
});
