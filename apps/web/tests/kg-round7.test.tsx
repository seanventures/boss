// @vitest-environment happy-dom
/**
 * THE GRAPH AREA, TESTER ROUND 6 P2s (fixed in round 7):
 *   1. nothing moves after the canvas is drawn: it waits for the setup strip's answer;
 *   2. the toolbar fits one row: zoom steps and List view are in the zoom menu;
 *   3/4. labels and search titles are cut at a word, the whole title in the tooltip;
 *   5. the tree, opened, is a column that pushes the canvas — never a drawer over it;
 *   6. the New project sheet is anchored at the top, so what it gains grows downward.
 */
import { readFileSync } from "node:fs";
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GraphArea } from "../app/knowledge/graph-area";
import { HIT_TITLE_MAX, hitTip, hitTitle } from "../app/knowledge/graph-view";
import { mockKgFetch } from "./_helpers/kg-fetch";
import { $, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }), usePathname: () => "/", useSearchParams: () => new URLSearchParams(window.location.search) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
beforeEach(() => { window.history.replaceState(null, "", "/?area=knowledge"); window.sessionStorage.clear(); window.localStorage.clear(); });
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const css = readFileSync("app/knowledge/kg.css", "utf8");
const globals = readFileSync("app/globals.css", "utf8");
const press = async (el: Element | null) => { await act(async () => (el as HTMLElement).click()); await settle(); };

describe("1. the canvas is drawn once what is above it is known", () => {
  it("shows the loading frame while the setup is unanswered, then the graph", async () => {
    const base = mockKgFetch();
    let release: (() => void) | null = null;
    const held = new Promise<void>((r) => { release = r; });
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      const href = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (href.includes("/v1/knowledge/setup")) { await held; }
      return base(input, init);
    });
    await mount(<GraphArea projectId="p" actor="ceo" />);
    expect($('[data-testid="kg-loading"]')).not.toBeNull();
    expect($('[data-testid="kg-graph"]')).toBeNull();
    await act(async () => { release!(); });
    await settle();
    expect($('[data-testid="kg-loading"]')).toBeNull();
    expect($('[data-testid="kg-graph"]')).not.toBeNull();
  });
});

describe("2. the toolbar's rarely used controls are in the zoom menu", () => {
  it("the trigger says the zoom; Zoom in, Zoom out and List view are in it", async () => {
    mockKgFetch();
    await mount(<GraphArea projectId="p" actor="ceo" />);
    expect($('[data-testid="kg-zoom-in"]')).toBeNull();
    expect($('[data-testid="kg-list-toggle"]')).toBeNull();
    expect($('[data-testid="kg-zoom-pct"]')?.textContent).toMatch(/^\d+%$/);
    expect($('[data-testid="kg-fit"]')).not.toBeNull();
    await press($('[data-testid="kg-view-menu"]'));
    const before = $('[data-testid="kg-zoom-pct"]')!.textContent;
    await press($('[data-testid="kg-zoom-in"]'));
    expect($('[data-testid="kg-zoom-pct"]')!.textContent).not.toBe(before);
    // A zoom step keeps the menu open; List view closes it.
    await press($('[data-testid="kg-list-toggle"]'));
    expect($('[data-testid="kg-zoom-in"]')).toBeNull();
    expect($('[data-testid="kg-zoom-pct"]')!.textContent).toBe("List");
  });
});

describe("4. search titles are cut at a word, the whole title in the tooltip", () => {
  it("cuts a long title at a word and keeps a short one whole", () => {
    const long = "Allocator that skips the remaining-amount decrement after paying principal, so the next allocation double-books the interest";
    const cut = hitTitle(long);
    expect(cut.length).toBeLessThanOrEqual(HIT_TITLE_MAX);
    expect(cut.endsWith("…")).toBe(true);
    expect(long.startsWith(cut.slice(0, -1))).toBe(true);
    expect(/[\s\-,]/.test(long[cut.length - 1] ?? " ")).toBe(true);
    expect(hitTitle("LoanCharge")).toBe("LoanCharge");
    expect(hitTip(long, "BOSS (general) · chat")).toBe(`${long}\nBOSS (general) · chat`);
  });
});

describe("5. the tree pushes the canvas over", () => {
  it("is a grid column wherever it opens, never absolutely placed over the canvas", () => {
    const medium = css.slice(css.indexOf("@container kg (max-width: 1180px)"), css.indexOf("@container kg (max-width: 860px)"));
    expect(medium).toMatch(/\.kg-work\[data-tree-open\][^{]*\{ grid-template-columns: min\(13rem, 22%\) minmax\(0, 1fr\) var\(--kg-panel-w\); \}/);
    expect(medium).not.toMatch(/\.kg-col-tree \{[^}]*position: absolute/);
    expect(css).toMatch(/--kg-tree-mode: opens/);
  });
});

describe("6. the New project sheet grows downward", () => {
  it("is anchored at the top, never centred", () => {
    expect(globals).toMatch(/\.bx-np-backdrop \{[^}]*align-items: start;/);
    expect(globals).not.toMatch(/\.bx-np-backdrop \{[^}]*place-items: center/);
  });
});
