/**
 * A chip's menu in New work stays inside the dialog (walk of rc10.36, 1440px).
 *
 * The menus hung from the left or right edge of their chip by counting chips (nth-child 4n,
 * 4n+3), as if every row held four. The wide Workflow chip takes two columns, so Quality sat in
 * the first column with a right-hung menu, and the dialog's left edge cut it off. Which edge is
 * now measured as the menu opens.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
const { popAlign } = await import("../app/projects/new-work");

const DIALOG = { left: 190, right: 1250 };

describe("which edge a chip's menu hangs from", () => {
  it("the first column (Quality at 1440): from its left edge, inside the dialog", () => {
    expect(popAlign({ left: 209, right: 459 }, 340, DIALOG)).toBe("start");
  });
  it("the last column (Tell): from its right edge", () => {
    expect(popAlign({ left: 982, right: 1231 }, 340, DIALOG)).toBe("end");
  });
  it("a middle chip whose menu fits either way opens from its left edge", () => {
    expect(popAlign({ left: 724, right: 973 }, 340, DIALOG)).toBe("start");
  });
  it("too wide for both: the side that overflows less", () => {
    expect(popAlign({ left: 300, right: 400 }, 1000, { left: 250, right: 1000 })).toBe("start");
    expect(popAlign({ left: 850, right: 950 }, 1000, { left: 0, right: 960 })).toBe("end");
  });
  it("the stylesheet no longer counts chips", () => {
    const css = readFileSync(join(__dirname, "../app/projects/new-work.css"), "utf8");
    expect(css).not.toMatch(/nth-child\([^)]*\)\s*\.px-nw-pop/);
    expect(css).toContain('.px-nw-pop[data-align="end"]');
    const tsx = readFileSync(join(__dirname, "../app/projects/new-work.tsx"), "utf8");
    expect(tsx).toContain('className="px-nw-pop" ref={placePop}');
  });
});
