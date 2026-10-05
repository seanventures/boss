/**
 * Talk to BOSS beside a narrow window: the sidebar folds to its icon rail while the dock is
 * open and the page column would fall under 720px, and comes back when it closes. The
 * operator's own collapse is separate state, never changed by this.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { MIN_PAGE_COLUMN, MIN_PAGE_WITH_DOCK, SIDEBAR_RAIL, dockWidthFor, railForDock } from "../lib/shell-rail";

describe("the dock makes room", () => {
  it("folds the sidebar when the dock would leave the page under 720px", () => {
    expect(railForDock({ dockOpen: true, viewport: 1024, dockWidth: 520 })).toBe(true);
    expect(railForDock({ dockOpen: true, viewport: 1280, dockWidth: 520 })).toBe(true);
    expect(railForDock({ dockOpen: true, viewport: 1600, dockWidth: 520 })).toBe(false);
    expect(railForDock({ dockOpen: true, viewport: 244 + 536 + MIN_PAGE_COLUMN, dockWidth: 520 })).toBe(false);
  });

  it("the dock is clamp(360, 40vw, 520), capped so the page keeps 560px beside the rail", () => {
    const page = (vw: number, w: number) => vw - SIDEBAR_RAIL - (w + 16);
    expect(dockWidthFor(1600)).toBe(520);
    expect(dockWidthFor(1280)).toBe(512);
    expect(page(1024, dockWidthFor(1024))).toBeGreaterThanOrEqual(MIN_PAGE_WITH_DOCK);
    expect(dockWidthFor(1024)).toBe(364);
    for (const vw of [1024, 1100, 1280, 1440, 1600]) {
      expect(page(vw, dockWidthFor(vw))).toBeGreaterThanOrEqual(MIN_PAGE_WITH_DOCK);
    }
  });

  it("a dragged width is kept, inside the same cap", () => {
    expect(dockWidthFor(1600, 700)).toBe(700);
    expect(dockWidthFor(1280, 700)).toBe(1280 - SIDEBAR_RAIL - 16 - MIN_PAGE_WITH_DOCK);
    expect(dockWidthFor(1600, 400)).toBe(400);
  });

  it("never folds it without the dock", () => {
    expect(railForDock({ dockOpen: false, viewport: 800, dockWidth: 520 })).toBe(false);
  });

  it("the shell applies it beside, never instead of, the operator's own collapse", () => {
    const shell = readFileSync("app/ConsoleShell.tsx", "utf8");
    expect(shell).toContain("const railed = docked || (railForChat && !keptOpen);");
    expect(shell).toContain('${railed ? " nav-docked" : ""}');
    // The remembered preference is written only by the operator's toggle.
    expect(shell.match(/localStorage\.setItem\("boss\.nav\.docked"/g)?.length).toBe(1);
    expect(shell).toContain("if (!dockedRun) { setRailForChat(false); setKeptOpen(false);");
  });
});
