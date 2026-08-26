import { existsSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { NotMovedYet } from "../components/v2/NotMovedYet";
import { V2_NAV } from "../components/v2/Shell";

/**
 * The nav must not offer anything it cannot deliver.
 *
 * Six of the nine entries pointed at routes that did not exist, so the sidebar offered six
 * links that 404. That is the same defect the operator opened this whole effort with — "fake
 * buttons" — reproduced inside the replacement for the console that had them.
 *
 * The fix is not to hide them (that loses the feature) and not to disable them (that is a dead
 * end). Every entry navigates somewhere real: a built screen, or a page that says what will
 * live there and links to the v1 area that has it today.
 */

describe("every nav entry goes somewhere real", () => {
  it.each(V2_NAV.map((n) => [n.id, n.href] as const))(
    "%s has a page on disk",
    (_id, href) => {
      const rel = href === "/v2" ? "app/v2/page.tsx" : `app${href}/page.tsx`;
      expect(existsSync(resolve(__dirname, "..", rel)), rel).toBe(true);
    },
  );

  it("every entry declares whether it has moved yet", () => {
    // An undeclared entry is one nobody decided about, which is how a 404 gets shipped.
    for (const item of V2_NAV) {
      expect(typeof item.built, item.id).toBe("boolean");
    }
  });

  it("every entry names the v1 area that has it today", () => {
    // This is what keeps the feature reachable during the move, and it is also the input to
    // the parity check.
    for (const item of V2_NAV) {
      expect(item.v1Area, item.id).toBeTruthy();
    }
  });

  it("the built ones are the ones actually built", () => {
    // If this drifts, the nav either hides a finished screen or promises an unfinished one.
    const built = V2_NAV.filter((n) => n.built).map((n) => n.id).sort();
    expect(built).toEqual(["boss", "factory", "hive", "infra", "missions", "org", "panels", "settings", "timeline"]);
  });
});

describe("a section that has not moved", () => {
  const html = renderToStaticMarkup(
    <NotMovedYet
      title="Workflows"
      summary="The delivery pipeline end to end."
      v1Area="delivery"
      orgId="org_boss"
    />,
  );

  it("says what will live there rather than shrugging", () => {
    expect(html).toContain("The delivery pipeline end to end.");
  });

  it("keeps the feature one click away", () => {
    // The difference between a placeholder and a dead end.
    expect(html).toContain('data-testid="v2-not-moved-link-delivery"');
    expect(html).toContain('href="/?area=delivery&amp;org=org_boss"');
  });

  it("keeps the org, so the link does not land in another company", () => {
    expect(html).toContain("org=org_boss");
  });

  it("says plainly that nothing was removed", () => {
    // "Coming soon" on a page where a working feature used to be reads as a loss.
    expect(html).toMatch(/Nothing has been removed/i);
  });

  it("does not claim a date it cannot keep", () => {
    expect(html).not.toMatch(/coming soon|next week|shortly|Q[1-4]/i);
  });
});
