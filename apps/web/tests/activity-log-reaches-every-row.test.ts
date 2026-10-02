import { describe, expect, it } from "vitest";

import { activityLogSearchParams, mapActivityFacets, mapActivityLog } from "../lib/activity-log";
import { codeOnly } from "./_helpers/source";

/**
 * Every row of the Activity log can be reached, and the chips count the log.
 *
 * boss-1, 2026-10-02: the page fetched the newest 200 rows with no way past them, and the
 * level/stream chips counted those 200 and read as totals (ERROR 0, Deadletters 0 while
 * Operations reported 14 dead-letters). The API now pages by cursor and counts per level and
 * stream (apps/api/tests/test_activity_counts_the_whole_log.py); these pin the console side.
 */
const UI = codeOnly("app/activity-log-ui.tsx");
const ROUTE = codeOnly("app/api/activity/route.ts");
const FACETS_ROUTE = codeOnly("app/api/activity/facets/route.ts");

describe("load older", () => {
  it("sends the cursor and reads next_cursor back", () => {
    expect(activityLogSearchParams("p", { cursor: "2026-09-01T00:00:00+00:00|evt_1" }).get("cursor"))
      .toBe("2026-09-01T00:00:00+00:00|evt_1");
    expect(mapActivityLog({ items: [], next_cursor: "c1" }, "p").nextCursor).toBe("c1");
    expect(mapActivityLog({ items: [] }, "p").nextCursor).toBeUndefined();
  });

  it("the route forwards the cursor — and the workflow-run scope it was dropping", () => {
    // workflow_run_id was sent by the console and silently dropped here, so arriving from a
    // workflow run showed the whole project's log.
    expect(ROUTE).toContain('"cursor"');
    expect(ROUTE).toContain('"workflow_run_id"');
  });

  it("offers Load older while there is an older page, with the same filters", () => {
    expect(UI).toContain('data-testid="activity-load-older"');
    expect(UI).toContain("{ ...listQuery, cursor: feed.nextCursor }");
  });

  it("says what failed and what to do when an older page cannot load", () => {
    expect(UI).toContain("press Load older again to retry");
  });
});

describe("the chips", () => {
  it("read the facets endpoint", () => {
    expect(FACETS_ROUTE).toContain("/v1/activity/facets");
    expect(UI).toContain("fetchActivityFacets(projectId");
  });

  it("mark a floor as a floor", () => {
    const facets = mapActivityFacets({ levels: { error: 14 }, sources: { deadletter: 14 },
                                       levels_exact: false, note: "newest 2000" });
    expect(facets).toEqual({ levels: { error: 14 }, sources: { deadletter: 14 },
                             levelsExact: false, note: "newest 2000" });
    expect(UI).toContain('!facets.levelsExact ? "+" : ""');
  });
});
