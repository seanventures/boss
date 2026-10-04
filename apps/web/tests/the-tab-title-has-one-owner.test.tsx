// @vitest-environment happy-dom
/**
 * The tab title's `(n)` has one owner (agents-home walk D5). The tester saw it missing on
 * Command Center and Observability, and gone after deciding until a reload: Next rewrites the
 * title on every navigation and refresh, and the pill wrote its prefix only when its count
 * changed. The shell now holds the count and puts the prefix back after any rewrite.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { titled, useTitleCount } from "../lib/tab-title";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
function Shell({ n }: { n: number }) { useTitleCount(n); return null; }
async function mount(n: number) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<Shell n={n} />));
}
const settle = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });
afterEach(() => { act(() => root?.unmount()); root = null; document.body.innerHTML = ""; });

describe("the tab title has one owner", () => {
  it("composes the prefix from the count", () => {
    expect(titled("BOSS", 2)).toBe("(2) BOSS");
    expect(titled("(5) BOSS", 0)).toBe("BOSS");
  });

  it("keeps (n) when a navigation rewrites the title, and follows the count without a reload", async () => {
    document.title = "BOSS Executive Console";
    await mount(3);
    expect(document.title).toBe("(3) BOSS Executive Console");
    // Next sets the page's metadata title again on navigating to another area, or a refresh.
    document.title = "BOSS Executive Console";
    await settle();
    expect(document.title).toBe("(3) BOSS Executive Console");
    // Deciding drops the count: the title follows at once.
    await act(async () => root!.render(<Shell n={2} />));
    expect(document.title).toBe("(2) BOSS Executive Console");
    await act(async () => root!.render(<Shell n={0} />));
    expect(document.title).toBe("BOSS Executive Console");
  });

  it("the pill no longer writes the title", async () => {
    const src = (await import("node:fs")).readFileSync("app/agents/presence-pill.tsx", "utf8");
    expect(src).not.toMatch(/document\.title\s*=/);
  });
});
