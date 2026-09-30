import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

/**
 * The card's "…" menu opened with its left half under the sidebar — unreadable, unclickable.
 *
 * Two independent faults, and the second is the one that matters:
 *
 * GEOMETRY. The menu was anchored `right: 0`, so a 264px panel grew 264px LEFTWARD from a
 * trigger sitting second-from-the-start of a left-aligned action row. Right-alignment is
 * correct for a trigger at the END of a row; this one is at the beginning, so the menu left the
 * card, crossed the gutter and ran under the nav.
 *
 * STACKING, which no amount of z-index could have fixed. `.bx-main` carried
 * `position: relative; z-index: 1`, and a positioned element with a z-index creates a STACKING
 * CONTEXT: everything inside is flattened to that one level in the root context. The sidebar is
 * z-index 2. So a menu inside main asking for z-index 40 was still painted underneath it, and
 * so was every other popover in the console — the ceiling was on the container, not the menu.
 *
 * Removing that z-index makes the whole main column compete honestly, which is why the mobile
 * ordering had to be revisited in the same change: the dock (60) would otherwise have covered
 * the slide-over nav (50).
 */
const CSS = readFileSync(join(process.cwd(), "app/globals.css"), "utf8");

/** The declarations of one selector's block. */
function rule(selector: string, { within = "" } = {}): string {
  const scope = within ? CSS.slice(CSS.indexOf(within)) : CSS;
  const at = scope.indexOf(`\n${selector} {`) >= 0
    ? scope.indexOf(`\n${selector} {`)
    : scope.indexOf(`${selector} {`);
  expect(at, `no rule for ${selector}`).toBeGreaterThan(-1);
  return scope.slice(at, scope.indexOf("}", at));
}

/** `z-index: N` from a block, or null. Comments are stripped so prose cannot match. */
function zIndex(block: string): number | null {
  const cleaned = block.replace(/\/\*[\s\S]*?\*\//g, " ");
  const m = cleaned.match(/z-index:\s*(-?\d+)/);
  return m ? Number(m[1]) : null;
}

describe("the main column does not trap what is inside it", () => {
  it("it creates no stacking context", () => {
    // This single declaration put a ceiling on every popover in the console.
    expect(zIndex(rule(".bx-main"))).toBeNull();
  });

  it("but still positions its own children", () => {
    expect(rule(".bx-main")).toContain("position: relative");
  });

  it("so a menu inside it can out-rank the sidebar", () => {
    const menu = zIndex(rule(".ct-menu"));
    const sidebar = zIndex(rule(".bx-sidebar"));
    expect(menu).not.toBeNull();
    expect(sidebar).not.toBeNull();
    expect(menu!).toBeGreaterThan(sidebar!);
  });
});

describe("the menu opens where there is room", () => {
  it("it is anchored to the left of its trigger, not the right", () => {
    // The trigger sits second in a left-aligned row, so growing rightward keeps the menu on
    // the card. Growing leftward is what put it under the nav.
    const menu = rule(".ct-menu");
    expect(menu).toContain("left: 0");
    expect(menu).toContain("right: auto");
  });

  it("and can never be wider than the viewport", () => {
    expect(rule(".ct-menu")).toMatch(/max-width:\s*min\(/);
  });

  it("the action row it belongs to is left-aligned, which is why", () => {
    // If this ever becomes a right-aligned row the anchoring should flip back — the assertion
    // exists so that change is a decision rather than a surprise.
    expect(rule(".ct-actions")).toContain("display: flex");
    expect(rule(".ct-actions")).not.toContain("justify-content: flex-end");
  });
});

describe("the mobile nav still wins", () => {
  const MOBILE = "@media (max-width: 960px)";

  it("the slide-over nav is above the dock", () => {
    // Previously guaranteed by the trap: the dock was flattened to main's level. With main
    // competing honestly the nav has to say so itself, or it slides in behind a full-width
    // panel on a phone.
    const nav = zIndex(rule(".bx-sidebar", { within: MOBILE }));
    const dock = zIndex(rule(".bx-dock"));
    expect(nav).not.toBeNull();
    expect(dock).not.toBeNull();
    expect(nav!).toBeGreaterThan(dock!);
  });

  it("and the scrim sits between them", () => {
    const nav = zIndex(rule(".bx-sidebar", { within: MOBILE }))!;
    const scrim = zIndex(rule(".bx-scrim", { within: MOBILE }))!;
    const dock = zIndex(rule(".bx-dock"))!;
    expect(scrim).toBeLessThan(nav);
    expect(scrim).toBeGreaterThan(dock);
  });
});
