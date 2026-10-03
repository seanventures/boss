import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The console has to fit the window it is given.
 *
 * Reported as "the UI is going out of the window". Measured on the deployed console: the page
 * itself did not scroll horizontally at 1504px, but two layouts had hard minimums that cannot
 * collapse, so below roughly 900px they push content past the right-hand edge:
 *
 *   .ct-split       grid-template-columns: minmax(280px, 360px) minmax(0, 1fr)
 *   .dw-canvas-head a row of fixed-width fields and nowrap buttons
 *
 * `.dw-builder` already collapsed at 900px. These did not, which is the whole bug: one part of
 * the same screen adapted and the rest did not.
 */
const CSS = codeOnly("app/globals.css");

/** The rules inside `@media (max-width: Npx) { … }`, so a rule can be asserted to live at a
 *  breakpoint rather than merely existing somewhere in the file. */
function atBreakpoint(px: number): string {
  const out: string[] = [];
  const needle = `@media (max-width: ${px}px)`;
  let from = 0;
  for (;;) {
    const start = CSS.indexOf(needle, from);
    if (start === -1) break;
    // Walk braces so a nested rule cannot end the block early.
    let depth = 0;
    let i = CSS.indexOf("{", start);
    const open = i;
    for (; i < CSS.length; i += 1) {
      if (CSS[i] === "{") depth += 1;
      else if (CSS[i] === "}") {
        depth -= 1;
        if (depth === 0) break;
      }
    }
    out.push(CSS.slice(open, i));
    from = i + 1;
  }
  return out.join("\n");
}

describe("the master-detail split collapses", () => {
  const narrow = atBreakpoint(900);

  it("stops being two columns when there is no room for two", () => {
    // minmax(280px, …) never collapses on its own: the list keeps its 280px whatever the
    // window does, and the detail is pushed off the edge.
    expect(CSS).toContain("grid-template-columns: minmax(280px, 360px) minmax(0, 1fr)");
    expect(narrow).toContain(".ct-split, .ct-split[data-docked=\"true\"] { grid-template-columns: minmax(0, 1fr); }");
  });

  it("unpins the list once it is stacked", () => {
    // A pane stuck to the top of a single-column page eats the screen the detail needs.
    expect(narrow).toContain(".ct-split-list { position: static; }");
  });

  it("drops the docked rail, which has nowhere to sit when stacked", () => {
    expect(narrow).toContain('.ct-split[data-docked="true"] .ct-split-list { display: none; }');
  });

  it("lets the detail's action buttons share the row", () => {
    // Each one forcing its own width is how the last button ends up off the edge.
    expect(narrow).toContain(".ct-detail-actions .cap-btn { flex: 1 1 auto; justify-content: center; }");
  });
});

describe("cards fit a phone", () => {
  const phone = atBreakpoint(560);

  it("stop being a 320px grid on a 360px screen", () => {
    // `minmax(320px, 1fr)` plus the page padding is wider than the viewport, so a single
    // column of cards still overflowed.
    expect(CSS).toContain("grid-template-columns: repeat(auto-fit, minmax(320px, 1fr))");
    expect(phone).toContain(".mr-grid, .ct-tiles { grid-template-columns: minmax(0, 1fr) !important; }");
  });

  it("give each action button the full width", () => {
    expect(phone).toContain(".ct-detail-actions .cap-btn { flex: 1 1 100%; }");
  });
});

describe("the breakpoints are the ones this console already uses", () => {
  it("does not invent a new one", () => {
    // 900px is the console's established breakpoint — `.dw-builder`, `.bx-dock` and the
    // loading bar all turn there. A layout that adapts at its own private width is a layout
    // that disagrees with the rest of the page halfway through a resize.
    expect(atBreakpoint(900).length).toBeGreaterThan(0);
    expect(CSS).toContain("@media (max-width: 900px) { .bx-dock { width: 100vw !important; } }");
  });
});
