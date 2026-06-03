import { describe, expect, it } from "vitest";
import { unavailableStatus } from "../lib/status";

describe("unavailableStatus", () => {
  it("marks API and database as unavailable", () => {
    const status = unavailableStatus("network down");

    expect(status.apiReachable).toBe(false);
    expect(status.databaseConnected).toBe(false);
    expect(status.blockedReason).toBe("network down");
  });
});
