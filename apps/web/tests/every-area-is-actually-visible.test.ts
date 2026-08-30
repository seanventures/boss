import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * An area that renders into the DOM and is hidden by CSS looks like a feature that was never
 * built.
 *
 * v1 shows one area at a time with a hardcoded per-area rule in globals.css. Adding an area
 * needs THREE things — the `AreaId` union, an entry in the nav list, and a line in that CSS —
 * and the third is invisible: the section is present, the nav item works, the URL is right,
 * and the page is blank. Nothing in the console says why.
 *
 * This has now cost two shipped-but-invisible areas. The list is derived, so a new area either
 * has its rule or fails here.
 */
const CSS = codeOnly("app/globals.css");
const PAGE = codeOnly("app/page.tsx");
const SHELL = codeOnly("app/ConsoleShell.tsx");

/** Every area the console can navigate to, from the nav definitions. */
function navAreas(): string[] {
  return [...SHELL.matchAll(/\{ id: "([a-z_]+)", label:/g)].map((m) => m[1]);
}

/** Every area that has a visibility rule. */
function visibleAreas(): Set<string> {
  return new Set(
    [...CSS.matchAll(/\.bx-content\[data-active="([a-z_]+)"\]/g)].map((m) => m[1]),
  );
}

/** Every area that actually renders a section in the page. */
function renderedAreas(): Set<string> {
  return new Set([...PAGE.matchAll(/data-area="([a-z_]+)"/g)].map((m) => m[1]));
}

describe("every navigable area can actually be seen", () => {
  it("has a visibility rule for each area that renders a section", () => {
    const rendered = renderedAreas();
    const visible = visibleAreas();
    const missing = [...rendered].filter((a) => !visible.has(a));
    expect(missing, `these areas render but CSS hides them: ${missing.join(", ")}`).toEqual([]);
  });

  it("has a visibility rule for each area in the nav", () => {
    // A nav item that leads to a blank page is worse than no nav item: it looks broken rather
    // than absent.
    const visible = visibleAreas();
    const rendered = renderedAreas();
    const missing = navAreas().filter((a) => rendered.has(a) && !visible.has(a));
    expect(missing, `nav leads to a hidden area: ${missing.join(", ")}`).toEqual([]);
  });

  it("covers the two areas that were shipped invisible", () => {
    // Named so a regression here reads as the specific bug it is, not as a generic failure.
    const visible = visibleAreas();
    expect(visible.has("delivery_workflows")).toBe(true);
    expect(visible.has("activity")).toBe(true);
  });

  it("finds enough areas to be a real check", () => {
    // A regex that stopped matching would make every assertion above vacuously true.
    expect(visibleAreas().size).toBeGreaterThan(15);
    expect(renderedAreas().size).toBeGreaterThan(10);
  });
});
