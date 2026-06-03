import { describe, expect, it } from "vitest";

import { emptyArtifactFeed } from "../lib/artifacts";
import { emptyActivityFeed } from "../lib/events";
import { emptyDeadletterFeed } from "../lib/deadletters";
import { emptyPackFeed } from "../lib/packs";
import { emptyRoleFeed } from "../lib/roles";

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

  it("returns an unavailable pack feed", () => {
    const feed = emptyPackFeed("pack api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("pack api down");
  });

  it("returns an unavailable role feed", () => {
    const feed = emptyRoleFeed("role api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("role api down");
  });
});
