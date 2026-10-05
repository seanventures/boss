// @vitest-environment happy-dom
/**
 * D10/D11 at the source (the browser walk measures the pixels): on a phone the Log draws
 * two-line rows whose height is the one the windowed list adds up, its drawer is a full-screen
 * sheet, range and Export sit behind ⋯; Govern keeps Evidence (stacked) and every target is
 * 44px. A CSS rule and the list's arithmetic that disagree would draw overlapping rows.
 */
import { readFileSync } from "node:fs";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { NARROW_PX, ROW_H_NARROW, flatten } from "../app/activity/log-model";
import { mapActivityLogItem } from "../lib/activity-log";
import { FACETS, fakeLogApi, newLog, rows } from "./_helpers/log-fixtures";
import { $, mount, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { LogExplorer } = await import("../app/activity/log-explorer");
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const LOG_CSS = readFileSync("app/activity/log.css", "utf8");
const GOV_CSS = readFileSync("app/govern/govern.css", "utf8");
const block = (css: string, at: string) => {
  const start = css.indexOf(at);
  expect(start, at).toBeGreaterThanOrEqual(0);
  let depth = 0;
  for (let i = css.indexOf("{", start); i < css.length; i += 1) {
    if (css[i] === "{") depth += 1;
    if (css[i] === "}") { depth -= 1; if (depth === 0) return css.slice(start, i + 1); }
  }
  return "";
};

describe("the Log on a phone", () => {
  it("a narrow list's row height is the one the windowed list adds up", () => {
    const narrow = block(LOG_CSS, `@container lx-list (max-width: ${NARROW_PX - 1}px)`);
    expect(narrow).toContain(`height: ${ROW_H_NARROW}px`);
    // The time sizes to itself with a real gap before the level ("01:10:09ⓘ Info" touched).
    expect(narrow).toMatch(/grid-template-columns: auto auto/);
    expect(Number(narrow.match(/column-gap: (\d+)px/)![1])).toBeGreaterThanOrEqual(8);
    const items = rows(3).flatMap(mapActivityLogItem);
    const flat = flatten(items, { byRun: false, open: new Set(), labels: {}, rowH: ROW_H_NARROW })
      .filter((r) => r.kind === "line");
    expect(flat.map((r) => r.height)).toEqual([ROW_H_NARROW, ROW_H_NARROW, ROW_H_NARROW]);
  });

  it("the drawer is a full-screen sheet and every target is 44px", () => {
    const phone = block(LOG_CSS, "@media (max-width: 719px)");
    expect(phone).toMatch(/\.lx-drawer \{[^}]*position: fixed;[^}]*inset: 0;/);
    expect(phone).toMatch(/min-height: 44px/);
  });

  it("range and Export sit behind ⋯", async () => {
    vi.stubGlobal("fetch", fakeLogApi(newLog({ page: { items: rows(2) }, facets: FACETS })));
    setConsoleLive(true);
    window.history.replaceState({}, "", "/?area=activity");
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    const tools = $("[data-testid=lx-head-tools]")!;
    expect(tools.querySelector("[data-testid=lx-range-24h]")).not.toBeNull();
    expect(tools.querySelector("[data-testid=lx-export]")).not.toBeNull();
    expect($("[data-testid=lx-more]")!.getAttribute("aria-label")).toBe("Range and export");
    expect(block(LOG_CSS, "@media (max-width: 719px)")).toMatch(/\.lx-head-tools \{ display: none; \}/);
  });
});

describe("the drawer on a phone", () => {
  it("is portalled to <body>, out of the console's transformed panel that trapped it", async () => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("max-width: 719px"), media: q,
      addEventListener: () => {}, removeEventListener: () => {} }));
    vi.stubGlobal("fetch", fakeLogApi(newLog({ page: { items: rows(2) }, facets: FACETS })));
    setConsoleLive(true);
    window.history.replaceState({}, "", "/?area=activity&row=r0");
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    const drawer = $("[data-testid=lx-drawer]")!;
    expect(drawer.parentElement!.className).toBe("lx lx-sheet-host");
    expect(drawer.parentElement!.parentElement).toBe(document.body);
    expect(drawer.closest(".lx-body")).toBeNull();
  });
});

describe("Govern on a phone", () => {
  it("never drops Evidence; it stacks", () => {
    for (const at of ["@media (max-width: 1023px)", "@media (max-width: 719px)"]) {
      expect(block(GOV_CSS, at)).not.toMatch(/\.gv-evidence \{[^}]*display: none/);
    }
  });
  it("every button, link, tab and field is 44px", () => {
    const phone = block(GOV_CSS, "@media (max-width: 719px)");
    expect(phone).toMatch(/\.gv button, \.gv a, \.gv select, \.gv input[^{]*\{[^}]*min-height: 44px/);
  });
});
