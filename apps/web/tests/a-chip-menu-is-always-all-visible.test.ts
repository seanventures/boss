// @vitest-environment happy-dom
/**
 * A chip's menu in New work is always all visible (re-walk of rc10.36 at 1440/1024/768).
 *
 * The Quality menu opened 316px tall below its chip with 258px left in the dialog's scrolling
 * body (.px-nw-body ends at y 765, the menu at 842), so Production and its note were cut off. Every
 * chip menu now measures as it opens: below if it fits, above if it fits there, else below with
 * the body scrolled by what is missing, capped to the body when taller than the body.
 */
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
const { placePop, popVertical } = await import("../app/projects/new-work");

type R = { top: number; bottom: number; left: number; right: number };
const rect = (r: R) => ({ ...r, x: r.left, y: r.top, width: r.right - r.left, height: r.bottom - r.top, toJSON() {} }) as DOMRect;

describe("popVertical", () => {
  const BODY = { top: 167, bottom: 765 };
  it("the walk's Quality menu (316px, 246px below, 248px above): below, the body scrolled by what is missing", () => {
    expect(popVertical({ top: 427, bottom: 507 }, 316, BODY)).toEqual({ side: "down", scrollBy: 70, maxHeight: null });
  });
  it("fits below: below, nothing moves", () => {
    expect(popVertical({ top: 300, bottom: 380 }, 316, BODY)).toEqual({ side: "down", scrollBy: 0, maxHeight: null });
  });
  it("fits only above: above", () => {
    expect(popVertical({ top: 600, bottom: 680 }, 316, BODY)).toEqual({ side: "up", scrollBy: 0, maxHeight: null });
  });
  it("taller than the body: capped to it", () => {
    const v = popVertical({ top: 427, bottom: 507 }, 900, BODY);
    expect(v.side).toBe("down");
    expect(v.maxHeight).toBe(586);
    expect(v.scrollBy).toBe(586 - 246);
  });
});

/** The dialog, its scrolling body, a chip and its menu, with the walk's geometry stubbed in. */
function stage(opts: { chip: R; pop: { w: number; h: number }; scrollRoom: number }) {
  const dialog = document.createElement("div"); dialog.className = "px-nw";
  const body = document.createElement("div"); body.className = "px-nw-body";
  const chip = document.createElement("div"); chip.className = "px-nw-def";
  const pop = document.createElement("div"); pop.className = "px-nw-pop";
  dialog.appendChild(body); body.appendChild(chip); chip.appendChild(pop); document.body.appendChild(dialog);
  let top = 0; // the body's scrollTop: chip and menu move up with it
  dialog.getBoundingClientRect = () => rect({ top: 40, bottom: 884, left: 190, right: 1250 });
  body.getBoundingClientRect = () => rect({ top: 167, bottom: 765, left: 190, right: 1250 });
  chip.getBoundingClientRect = () => rect({ ...opts.chip, top: opts.chip.top - top, bottom: opts.chip.bottom - top });
  // The menu moves up with the body's scroll, as it does inside a scrolling element.
  pop.getBoundingClientRect = () => rect({ top: opts.chip.bottom + 6 - top, bottom: opts.chip.bottom + 6 + opts.pop.h - top,
                                           left: opts.chip.left, right: opts.chip.left + opts.pop.w });
  // A body that can scroll only `scrollRoom` px further.
  Object.defineProperty(body, "scrollTop", { get: () => top, set: (v: number) => { top = Math.min(v, opts.scrollRoom); } });
  return { dialog, body, pop };
}

describe("placePop, on open", () => {
  it("the Quality menu: left edge inside the dialog, the body scrolled so its bottom shows", () => {
    const { body, pop, dialog } = stage({ chip: { top: 427, bottom: 507, left: 209, right: 459 }, pop: { w: 340, h: 316 }, scrollRoom: 400 });
    placePop(pop);
    expect(pop.dataset.align).toBe("start");
    expect(pop.dataset.side).toBe("down");
    expect(body.scrollTop).toBe(70);
    expect(pop.style.getPropertyValue("--px-nw-pop-max")).toBe("");
    dialog.remove();
  });
  it("a body that cannot scroll far enough: the rest is capped and scrolls inside the menu", () => {
    const { body, pop, dialog } = stage({ chip: { top: 427, bottom: 507, left: 209, right: 459 }, pop: { w: 340, h: 316 }, scrollRoom: 20 });
    placePop(pop);
    expect(body.scrollTop).toBe(20);
    // Menu top 513 - 20 = 493; the body's edge 765 - 6 = 759: 266px show, the rest scrolls inside.
    expect(pop.style.getPropertyValue("--px-nw-pop-max")).toBe("266px");
    dialog.remove();
  });
  it("a chip half under the body's bottom edge: brought into view, then its menu placed", () => {
    // Chip 740-820 under a body ending at 765: scrolled 61 (820 - 765 + 6) to 679-759, then the
    // menu fits above it (679 - 167 - 12 = 500 >= 316).
    const { body, pop, dialog } = stage({ chip: { top: 740, bottom: 820, left: 982, right: 1231 }, pop: { w: 340, h: 316 }, scrollRoom: 400 });
    placePop(pop);
    expect(body.scrollTop).toBe(61);
    expect(pop.dataset.side).toBe("up");
    expect(pop.dataset.align).toBe("end");
    dialog.remove();
  });
  it("the stylesheet opens upward on data-side and honours the cap", async () => {
    const { readFileSync } = await import("node:fs");
    const { join } = await import("node:path");
    const css = readFileSync(join(__dirname, "../app/projects/new-work.css"), "utf8");
    expect(css).toContain('.px-nw-pop[data-side="up"] { top: auto; bottom: calc(100% + 6px); }');
    expect(css).toContain("max-height: var(--px-nw-pop-max, none)");
  });
});
