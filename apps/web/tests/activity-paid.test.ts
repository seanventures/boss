import { describe, expect, it } from "vitest";

import { emptyCommercialFeed } from "../lib/commercial";
import { emptyEnterpriseFeed } from "../lib/enterprise";

describe("paid API feeds", () => {
  it("returns an unavailable commercial feed", () => {
    const feed = emptyCommercialFeed("commercial api down");
    expect(feed.apiReachable).toBe(false);
    expect(feed.entitlements).toEqual([]);
    expect(feed.blockedReason).toBe("commercial api down");
  });

  it("returns an unavailable enterprise feed", () => {
    const feed = emptyEnterpriseFeed("enterprise api down");
    expect(feed.apiReachable).toBe(false);
    expect(feed.clusters).toEqual([]);
    expect(feed.scimUsers).toEqual([]);
    expect(feed.summary.blockingGapCount).toBe(0);
    expect(feed.blockedReason).toBe("enterprise api down");
  });
});
