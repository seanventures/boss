import { describe, expect, it } from "vitest";

import { emptyBreakGlassGrantFeed } from "../lib/break-glass";

describe("break-glass feed", () => {
  it("returns a visible unavailable state when the break-glass API is unreachable", () => {
    const feed = emptyBreakGlassGrantFeed("break glass api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("break glass api down");
  });
});
