import { describe, expect, it } from "vitest";

import { mapActivityFacets, mapActivityLog } from "../lib/activity-log";
import { LOG_FORWARD } from "../lib/activity-proxy";
import { codeOnly } from "./_helpers/source";

/**
 * Every row of the Activity log can be reached, and the rail counts the log.
 *
 * boss-1, 2026-10-02: the page fetched the newest 200 rows with no way past them, and the chips
 * counted those 200 and read as totals (ERROR 0, Deadletters 0 while Operations reported 14
 * dead-letters). The API pages by keyset and counts in SQL
 * (apps/api/tests/test_the_log_reaches_every_row_once.py); these pin the console side.
 */
const EXPLORER = codeOnly("app/activity/log-explorer.tsx");
const RAIL = codeOnly("app/activity/facet-rail.tsx");

describe("older lines", () => {
  it("the cursor and the tail's `after` go through the route, and come back mapped", () => {
    expect(LOG_FORWARD as readonly string[]).toEqual(expect.arrayContaining(["cursor", "after", "workflow_run_id"]));
    const page = mapActivityLog({ items: [], next_cursor: "c1", newest_cursor: "n1", gap: true }, "p");
    expect([page.nextCursor, page.newestCursor, page.gap]).toEqual(["c1", "n1", true]);
    expect(mapActivityLog({ items: [] }, "p").nextCursor).toBeUndefined();
  });

  it("scrolling near the end loads older lines, and a Load older button stays for the keyboard", () => {
    expect(EXPLORER).toContain("onNearEnd={() => { if (stream.next");
    expect(EXPLORER).toContain('data-testid="lx-older"');
    expect(EXPLORER).toContain("Start of range");
  });
});

describe("the rail counts the log, and says when a count is a floor", () => {
  it("maps every facet the API counts", () => {
    const f = mapActivityFacets({ levels: { error: 14 }, sources: { deadletter: 14 }, statuses: { failed: 14 },
                                   services: { worker: 3 }, agents: [{ role_key: "qa_engineer", n: 2 }],
                                   runs: [{ id: "wfr_1", label: "Ship it", n: 9 }], total: 30, exact: false })!;
    expect(f.levels.error).toBe(14);
    expect(f.sources.deadletter).toBe(14);
    expect(f.agents).toEqual([{ roleKey: "qa_engineer", n: 2 }]);
    expect(f.runs).toEqual([{ id: "wfr_1", label: "Ship it", n: 9 }]);
    expect(f.exact).toBe(false);
    expect(mapActivityFacets(null)).toBeNull();
  });

  it("a floor is shown with ≥ and its reason in the tooltip", () => {
    expect(RAIL).toContain('`${exact ? "" : "≥"}');
    expect(RAIL).toContain("At least this many. Counting stopped early.");
  });

  it("the honesty fields the old mapper dropped are kept", () => {
    const page = mapActivityLog({ items: [], total: 50, total_is_exact: false, truncated_note: "floor" }, "p");
    expect([page.total, page.totalIsExact, page.truncatedNote]).toEqual([50, false, "floor"]);
  });
});
