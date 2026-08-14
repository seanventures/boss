import { describe, expect, it } from "vitest";

import { emptyWipStatus } from "../lib/factory";

describe("Factory WIP feed honesty", () => {
  it("does not collapse a fetch failure into a silent empty slot", () => {
    const wip = emptyWipStatus("API returned 503");
    expect(wip.apiReachable).toBe(false);
    expect(wip.blockedReason).toBe("API returned 503");
    expect(wip.admit).toBe(false);
    expect(wip.fixtureSupported).toBe(false);
    expect(wip.inFlight).toBe(0);
  });
});
