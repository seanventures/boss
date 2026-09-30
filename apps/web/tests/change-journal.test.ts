import { describe, expect, it } from "vitest";

import { emptyChangeJournalFeed } from "../lib/change-journal";

describe("emptyChangeJournalFeed", () => {
  it("marks the change journal API as unavailable and returns no items", () => {
    const feed = emptyChangeJournalFeed("api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("api down");
  });
});
