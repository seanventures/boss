import { describe, expect, it } from "vitest";
import { emptyDecisionFeed } from "../lib/decisions";

describe("emptyDecisionFeed", () => {
  it("marks the decision API as unavailable and returns no cards", () => {
    const feed = emptyDecisionFeed("api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("api down");
  });
});
