import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { activeAreaId, shouldRenderSection } from "../lib/area-visibility";

/**
 * The console's weight was never its data.
 *
 * Measured: 6.38 MB of payload, of which 2.04 MB (32%) is `className` — class names, not data.
 * `page.tsx` built all 64 top-level sections on every request, ConsoleShell is a client
 * component so its children cross the boundary as PROPS and every one was serialised, and CSS
 * then hid ~63 of them. Gating the data fetches could not touch that, and did not: it moved
 * 8.38 MB to 8.23 MB.
 */

const PAGE = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
const CSS = readFileSync(resolve(__dirname, "../app/globals.css"), "utf8");

describe("the rule", () => {
  it("renders a section whose area is the active one", () => {
    expect(shouldRenderSection("budget", ["budget"])).toBe(true);
  });

  it("skips a section belonging to some other area", () => {
    expect(shouldRenderSection("budget", ["infra"])).toBe(false);
  });

  it("treats a missing area as overview, matching the CSS default", () => {
    expect(activeAreaId(undefined)).toBe("overview");
    expect(activeAreaId("")).toBe("overview");
    expect(shouldRenderSection(undefined, ["overview"])).toBe(true);
    expect(shouldRenderSection(undefined, ["budget"])).toBe(false);
  });

  it("renders a section that names no area at all", () => {
    // Chrome, not an area. Hiding it would be a guess, and failing toward rendering keeps the
    // failure mode "still too big" rather than "a panel vanished".
    expect(shouldRenderSection("budget", [])).toBe(true);
  });

  it("renders a section that lists the active area among several", () => {
    expect(shouldRenderSection("infra", ["budget", "infra"])).toBe(true);
  });
});

describe("the wiring", () => {
  it("guards every top-level section", () => {
    const sections = PAGE.split("\n").filter(
      (l) => l.startsWith(" ".repeat(8) + "<section") && !l.startsWith(" ".repeat(9)),
    ).length;
    const guards = (PAGE.match(/shouldRenderSection\(requestedArea, \[/g) ?? []).length;
    expect(guards).toBe(sections);
    expect(sections).toBeGreaterThan(40);
  });

  it("mirrors the CSS rather than inventing a second rule", () => {
    // The CSS identifies a section by its OWN data-area or a descendant's (`:has`). Sixteen
    // sections carry no data-area of their own — three of them spanning 1027, 451 and 343
    // lines — so an attribute-only rule would have skipped exactly the sections worth skipping.
    expect(CSS).toContain(":has([data-area=");
    const areasInCss = new Set(
      [...CSS.matchAll(/\.bx-content\[data-active="([a-zA-Z0-9_-]+)"\]/g)].map((m) => m[1]),
    );
    const areasInGuards = new Set(
      [...PAGE.matchAll(/shouldRenderSection\(requestedArea, \[([^\]]+)\]/g)]
        .flatMap((m) => m[1].split(",").map((s) => s.trim().replace(/"/g, ""))),
    );

    // Two PRE-EXISTING mismatches, found by this test and confirmed against main:
    //   ai-sre — a section with no CSS rule. globals.css hides every section by default and
    //            reveals only the listed areas, so this one has NEVER been visible, while
    //            being rendered and serialised on every page load.
    //   github — a CSS rule with no section, so ?area=github shows an empty area.
    // Pinned rather than excused: if either is fixed, or a THIRD appears, this fails and says so.
    const KNOWN_UNSTYLED = ["ai-sre"];
    const KNOWN_EMPTY = ["github"];

    expect(
      [...areasInGuards].filter((a) => !areasInCss.has(a)).sort(),
      "an area gated in the render with no CSS rule can never be shown",
    ).toEqual(KNOWN_UNSTYLED);
    expect(
      [...areasInCss].filter((a) => !areasInGuards.has(a)).sort(),
      "an area with a CSS rule and no section renders empty",
    ).toEqual(KNOWN_EMPTY);
  });

  it("still loads only the active area's data", () => {
    // R1's first slice. Rendering one area is pointless while every area's data is fetched.
    expect(PAGE).toContain("shouldLoadBudgetArea(requestedArea)");
    expect(PAGE).toContain("shouldLoadInfraArea(requestedArea)");
    expect(PAGE).toContain("shouldLoadAssuranceArea(requestedArea)");
  });
});
