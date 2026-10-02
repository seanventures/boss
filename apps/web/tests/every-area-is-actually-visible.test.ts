import { describe, expect, it } from "vitest";

import { AREA_IDS } from "../lib/area-ids";
import { areaVisibilityCss } from "../lib/area-visibility";
import { codeOnly, cssOnly } from "./_helpers/source";

/**
 * An area that renders into the DOM and is hidden by CSS looks like a feature that was never
 * built.
 *
 * v1 shows one area at a time. The rule that reveals an area used to be a hand-written line per
 * area in globals.css, and it drifted three times: Workflow and Activity log shipped invisible,
 * then AI SRE did — reachable through the command palette and ?area=ai-sre, and blank (boss-1:
 * 0 visible sections). The previous version of this test could not see that one, because its
 * regexes were `[a-z_]+` and the id has a hyphen in it.
 *
 * REWRITTEN for the new design: the rule is now generated from the AREAS list the rail is built
 * from (lib/area-visibility.ts), so these checks pin the generation, enumerate every id in the
 * AreaId union, and forbid the hand-written list from coming back.
 */
const CSS = cssOnly("app/globals.css");
const PAGE = codeOnly("app/page.tsx");
const SHELL = codeOnly("app/ConsoleShell.tsx");

/** Area ids are lowercase words joined by `_` or `-` — the hyphen is the one that was missed. */
const ID = "[a-z][a-z0-9_-]*";

/** Every area the console can navigate to, from the nav definitions. */
function navAreas(): string[] {
  return [...SHELL.matchAll(new RegExp(`\\{\\s*id: "(${ID})",\\s*label:`, "g"))].map((m) => m[1]);
}

/** Every AreaId. The ids moved out of a type union in ConsoleShell into lib/area-ids.ts (a value
 *  and a type) so link targets can be checked; this reads the same list from there. */
function unionAreas(): string[] {
  return [...AREA_IDS];
}

/** Every area that actually renders a section in the page. */
function renderedAreas(): Set<string> {
  return new Set([...PAGE.matchAll(new RegExp(`data-area="(${ID})"`, "g"))].map((m) => m[1]));
}

describe("every navigable area can actually be seen", () => {
  it("generates a visibility rule for every area in the nav, hyphenated ids included", () => {
    const css = areaVisibilityCss(navAreas());
    for (const id of navAreas()) {
      expect(css, `no rule for ${id}`).toContain(`.bx-content[data-active="${id}"]`);
      expect(css).toContain(`[data-area="${id}"]`);
    }
    expect(css).toContain('[data-active="ai-sre"]');
  });

  it("has every AreaId in the nav list the rule is generated from", () => {
    const nav = new Set(navAreas());
    const missing = unionAreas().filter((id) => !nav.has(id));
    expect(missing, `AreaId members with no AREAS entry (so no rule): ${missing.join(", ")}`)
      .toEqual([]);
  });

  it("has an AREAS entry for each area that renders a section", () => {
    const nav = new Set(navAreas());
    const orphaned = [...renderedAreas()].filter((id) => !nav.has(id));
    expect(orphaned, `these render but have no rule: ${orphaned.join(", ")}`).toEqual([]);
  });

  it("the shell renders the generated rule from AREAS", () => {
    expect(SHELL).toContain("areaVisibilityCss(AREAS.map((a) => a.id))");
    expect(SHELL).toMatch(/<style[^>]*dangerouslySetInnerHTML=\{\{ __html: AREA_VISIBILITY_CSS \}\}/);
  });

  it("globals.css no longer carries a hand-written per-area visibility list", () => {
    // A hand-written line would compete with the generated one and drift again.
    const handWritten = [...CSS.matchAll(
      new RegExp(`\\.bx-content\\[data-active="(${ID})"\\] \\.frame > section:is`, "g"),
    )].map((m) => m[1]);
    expect(handWritten).toEqual([]);
  });

  it("refuses an id that would make a broken selector", () => {
    expect(() => areaVisibilityCss(['bad"id'])).toThrow(/cannot be used/);
  });

  it("finds enough areas to be a real check", () => {
    // A regex that stopped matching would make every assertion above vacuously true.
    expect(navAreas().length).toBeGreaterThan(30);
    expect(unionAreas().length).toBeGreaterThan(30);
    expect(renderedAreas().size).toBeGreaterThan(10);
    expect(renderedAreas().has("ai-sre")).toBe(true);
  });
});
