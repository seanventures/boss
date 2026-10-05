/**
 * Talk to BOSS beside a narrow window: the sidebar folds to its icon rail while the dock is
 * open and the page column would fall under 720px, and comes back when it closes. The
 * operator's own collapse is separate state, never changed by this.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { MIN_PAGE_COLUMN, railForDock } from "../lib/shell-rail";

describe("the dock makes room", () => {
  it("folds the sidebar when the dock would leave the page under 720px", () => {
    expect(railForDock({ dockOpen: true, viewport: 1024, dockWidth: 520 })).toBe(true);
    expect(railForDock({ dockOpen: true, viewport: 1280, dockWidth: 520 })).toBe(true);
    expect(railForDock({ dockOpen: true, viewport: 1600, dockWidth: 520 })).toBe(false);
    expect(railForDock({ dockOpen: true, viewport: 244 + 536 + MIN_PAGE_COLUMN, dockWidth: 520 })).toBe(false);
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
