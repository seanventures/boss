import { describe, expect, it } from "vitest";

import { emptyDashboardFeed } from "../lib/dashboard";

describe("Dashboard feed honesty", () => {
  it("keeps the blocked reason instead of returning a silent null", () => {
    const feed = emptyDashboardFeed("API returned 500");
    expect(feed.apiReachable).toBe(false);
    expect(feed.snapshot).toBeNull();
    expect(feed.blockedReason).toBe("API returned 500");
  });
});
