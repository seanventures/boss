import { describe, expect, it } from "vitest";

import { emptyArtifactFeed } from "../lib/artifacts";
import { emptyActivityFeed } from "../lib/events";
import { emptyDeadletterFeed } from "../lib/deadletters";
import { emptyEntitlementFeed } from "../lib/entitlements";
import { emptyPackFeed } from "../lib/packs";
import { emptyRoleFeed } from "../lib/roles";
import { emptyRoutingLogFeed } from "../lib/routing";
import { emptySkillFeed } from "../lib/skills";

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

  it("returns an unavailable entitlement feed", () => {
    const feed = emptyEntitlementFeed("entitlement api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("entitlement api down");
  });

  it("returns an unavailable routing log feed", () => {
    const feed = emptyRoutingLogFeed("routing api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("routing api down");
  });

  it("returns an unavailable skill feed", () => {
    const feed = emptySkillFeed("skill api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("skill api down");
  });
});
