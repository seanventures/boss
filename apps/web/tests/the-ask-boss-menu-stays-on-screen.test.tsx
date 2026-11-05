// @vitest-environment happy-dom
/**
 * The Observability Ask BOSS menu is anchored to the trigger's right edge, so it grows left.
 * On a wrapped header the trigger sits near the left of the row (measured at 1680px: the
 * button's right edge is 334.5px). A 420px menu then starts at -85.5px, off the left of the
 * screen. It has to open toward the room that is there, and a trigger at the right of a wide
 * window still opens left so it does not leave the right edge either.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { askMenuSide } from "../app/observability/obs-util";
import { $, click, unmount } from "./_helpers/live-render";
import { openObs } from "./_helpers/obs-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const MENU = 420;

describe("the Ask BOSS menu stays on the screen", () => {
  it("grows right when growing left would leave the screen", () => {
    // Wrapped header, 1680px window, trigger right edge 334.5. Growing left: 334.5 - 420 = -85.5.
    expect(askMenuSide({ left: 243.6, right: 334.5 }, 1680, MENU)).toBe("start");
    expect(243.6).toBeGreaterThanOrEqual(0);
    expect(243.6 + MENU).toBeLessThanOrEqual(1680);
  });

  it("grows left when the trigger is at the right edge", () => {
    // One row, 1920px window, trigger right edge 1892. Growing right: 1801 + 420 = 2221, past 1920.
    expect(askMenuSide({ left: 1801, right: 1892 }, 1920, MENU)).toBe("end");
    expect(1892 - MENU).toBeGreaterThanOrEqual(0);
    expect(1892).toBeLessThanOrEqual(1920);
  });

  it("the open menu takes that side", async () => {
    await openObs("?view=apps");
    const button = $("[data-testid='obs-ask']")!;
    const pop = button.parentElement!;
    pop.getBoundingClientRect = () => ({
      x: 243.6, y: 62, left: 243.6, right: 334.5, top: 62, bottom: 94, width: 90.9, height: 32, toJSON() { return {}; },
    });
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1680 });
    await click(button);
    expect($("[data-testid='obs-ask-menu']")?.getAttribute("data-side")).toBe("start");
  });

  it("a trigger at the right edge still opens left", async () => {
    await openObs("?view=apps");
    const button = $("[data-testid='obs-ask']")!;
    const pop = button.parentElement!;
    pop.getBoundingClientRect = () => ({
      x: 1801, y: 18, left: 1801, right: 1892, top: 18, bottom: 50, width: 91, height: 32, toJSON() { return {}; },
    });
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1920 });
    await click(button);
    expect($("[data-testid='obs-ask-menu']")?.getAttribute("data-side")).toBe("end");
  });

  it("the start side is the trigger's left edge, so the menu grows into the page", () => {
    const css = [
      readFileSync(join(process.cwd(), "app/infra/infra.css"), "utf8"),
      readFileSync(join(process.cwd(), "app/observability/obs.css"), "utf8"),
    ].join("\n");
    const style = document.createElement("style");
    style.textContent = css;
    document.head.appendChild(style);
    const menu = document.createElement("span");
    menu.className = "inf-menu obs-ask-menu";
    menu.dataset.side = "start";
    document.body.appendChild(menu);
    const placed = getComputedStyle(menu);
    expect(placed.left).toBe("0px");
    expect(placed.right).toBe("auto");
  });
});
