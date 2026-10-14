// @vitest-environment happy-dom
/**
 * THE PALETTE'S ENTER TAKES THE FIRST CHOICE SHOWN (rc10.5 walk m2). Enter acted on areas only,
 * so typing "new build" and pressing Enter did nothing: New build is a command, not an area.
 * Enter now presses the first item the list shows, whatever kind, exactly as a click would.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
                                    usePathname: () => "/", useSearchParams: () => new URLSearchParams() }));
const { activatePaletteChoice } = await import("../app/ConsoleShell");

function list(items: string[]) {
  const root = document.createElement("div");
  const pressed: string[] = [];
  for (const id of items) {
    const b = document.createElement("button");
    b.className = "bx-palette-item";
    b.dataset.testid = id;
    b.addEventListener("click", () => pressed.push(id));
    root.appendChild(b);
  }
  return { root, pressed };
}

describe("palette Enter", () => {
  it("presses New build when it is the first choice shown", () => {
    const { root, pressed } = list(["palette-new-build", "palette-converse-talk"]);
    expect(activatePaletteChoice(root)).toBe(true);
    expect(pressed).toEqual(["palette-new-build"]);
  });

  it("presses an area when one is first, and nothing when the list is empty", () => {
    const { root, pressed } = list(["area-overview", "palette-new-build"]);
    activatePaletteChoice(root);
    expect(pressed).toEqual(["area-overview"]);
    expect(activatePaletteChoice(document.createElement("div"))).toBe(false);
    expect(activatePaletteChoice(null)).toBe(false);
  });

  it("the shell's input uses it, not an areas-only rule", () => {
    const src = readFileSync(resolve(__dirname, "../app/ConsoleShell.tsx"), "utf8");
    expect(src).toContain("activatePaletteChoice(paletteList.current)");
    expect(src).not.toContain('if (e.key === "Enter" && filtered[0]) go(filtered[0].id)');
  });
});
