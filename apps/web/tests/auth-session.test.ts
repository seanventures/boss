import { describe, expect, it } from "vitest";

import { emptyAuthSession } from "../lib/auth-session";

describe("auth session feed", () => {
  it("returns a visible unavailable state when the session API is unreachable", () => {
    const feed = emptyAuthSession("auth api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.authRequired).toBe(false);
    expect(feed.actorBindingRequired).toBe(false);
    expect(feed.blockedReason).toBe("auth api down");
  });
});
