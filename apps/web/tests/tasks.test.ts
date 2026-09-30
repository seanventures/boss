import { describe, expect, it } from "vitest";

import { emptyTaskFeed } from "../lib/tasks";

describe("emptyTaskFeed", () => {
  it("marks the task API as unavailable and returns no tasks", () => {
    const feed = emptyTaskFeed("api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("api down");
  });
});
