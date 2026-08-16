import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { communityNavCopy, isCommunityPrimaryNavVisible } from "../lib/edition";

/**
 * What Community shows, on one codebase.
 *
 * The alternative was a second console with its own nav and shell. It was built, measured and
 * reverted: two nav definitions means the moment a feature lands in one, the other is wrong.
 * Community is the ungated subset of the same console, so work on either edition lands in
 * both — which is worth more than the tidiness of a fresh start.
 *
 * Hidden, not deleted. Every area below is real and the paid editions need it; removing them
 * to slim Community is exactly what creates the drift. Deep links and the command palette
 * still reach them.
 */

const AREAS = readFileSync(resolve(__dirname, "../app/ConsoleShell.tsx"), "utf8");

/** Every area id the console defines, read from the nav table itself. */
function allAreaIds(): string[] {
  return [...AREAS.matchAll(/\{ id: "([a-z_]+)", label:/g)].map((m) => m[1]);
}

const COMMUNITY = "community";
const ENTERPRISE = "enterprise";

describe("what Community is offered", () => {
  it("hides the areas that are enterprise surfaces or unfinished", () => {
    for (const hidden of [
      "foundations", "components", "overview", "capabilities", "studio", "sandboxes",
      "enterprise", "metatron", "activity", "portfolio", "processes", "reliability",
      "comms", "assurance", "org",
    ]) {
      expect(isCommunityPrimaryNavVisible(hidden, COMMUNITY), `${hidden} should be hidden`)
        .toBe(false);
    }
  });

  it("keeps the areas a self-hosted user actually works in", () => {
    for (const kept of [
      "agent", "dashboard", "decisions", "work", "discovery", "meetings", "code_tasks",
      "delivery", "monitoring", "shepherd", "reports", "budget", "pipeline", "ops",
      "github", "infra",
    ]) {
      expect(isCommunityPrimaryNavVisible(kept, COMMUNITY), `${kept} should be visible`)
        .toBe(true);
    }
  });

  it("leaves the meeting and discovery flow in place", () => {
    // The point of the trim is to make the path from idea to shipped software findable, not
    // to remove steps from it. Hiding these would cut the flow it exists to expose.
    for (const step of ["discovery", "meetings", "code_tasks", "delivery"]) {
      expect(isCommunityPrimaryNavVisible(step, COMMUNITY)).toBe(true);
    }
  });

  it("changes nothing for the paid editions", () => {
    for (const id of allAreaIds()) {
      expect(isCommunityPrimaryNavVisible(id, ENTERPRISE), `${id} hidden on enterprise`)
        .toBe(true);
    }
  });

  it("calls Evidence what an operator would look for", () => {
    expect(communityNavCopy("reports").label).toBe("Reports");
  });

  it("cuts the nav roughly in half without emptying it", () => {
    const visible = allAreaIds().filter((id) => isCommunityPrimaryNavVisible(id, COMMUNITY));
    expect(visible.length).toBeGreaterThan(12);
    expect(visible.length).toBeLessThan(20);
  });
});
