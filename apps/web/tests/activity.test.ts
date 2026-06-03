import { describe, expect, it } from "vitest";

import { emptyArtifactFeed } from "../lib/artifacts";
import { emptyActivityFeed } from "../lib/events";
import { emptyDeadletterFeed } from "../lib/deadletters";

describe("empty API feeds", () => {
  it("returns an unavailable artifact feed", () => {
    const feed = emptyArtifactFeed("artifact api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("artifact api down");
  });

  it("returns an unavailable activity feed", () => {
    const feed = emptyActivityFeed("event api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("event api down");
  });

  it("returns an unavailable deadletter feed", () => {
    const feed = emptyDeadletterFeed("deadletter api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("deadletter api down");
  });
});
