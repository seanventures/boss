// @vitest-environment happy-dom
/**
 * Every panel that drops from the top bar keeps its own width (operator screenshot, 2026-11-18).
 *
 * "Find a build" opened as a ~130px sliver under the Builds chip: the search box showed "F", the
 * tabs ran out of the panel to the right, and every row read "Sh". The picker is a child of the
 * chip (position: relative), so the chip is its containing block, and its width was
 * `min(520px, calc(100% - 40px))`: 100% of a 130px trigger. The bell's crew and the avatar menu
 * sit the same way inside their triggers, so the rule is held for all three: a top-bar panel is
 * sized by its own token and the viewport, never by a percentage, and it lays out its own content.
 */
import React from "react";
import { readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { declarations } from "./_helpers/css-declarations";
import { cssOnly } from "./_helpers/source";
import { $, $$, cleanup, click, render } from "./_helpers/kit-render";

const ROOT = join(__dirname, "..");
const PANELS = [".bx-project-picker", ".ag-crew", ".idn-me-menu"];

function stylesheets(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (name === "node_modules" || name.startsWith(".")) continue;
    if (statSync(p).isDirectory()) out.push(...stylesheets(p));
    else if (name.endsWith(".css")) out.push(p);
  }
  return out;
}

describe("a top-bar panel is never sized by its trigger", () => {
  it("no rule gives a top-bar panel a percentage width (it would resolve against the trigger)", () => {
    const offenders: string[] = [];
    for (const file of [...stylesheets(join(ROOT, "app")), ...stylesheets(join(ROOT, "components"))]) {
      for (const d of declarations(cssOnly(file))) {
        if (!/^(min-|max-)?width$/.test(d.prop)) continue;
        // The panel itself (the last compound of a selector), not a row inside it: a row's 100%
        // is of the panel, which is what it should be.
        const own = d.selector.split(",").some((part) => {
          const last = part.trim().split(/\s+|>/).filter(Boolean).pop() ?? "";
          return PANELS.some((p) => new RegExp(`\\${p}(?![\\w-])`).test(last));
        });
        if (!own) continue;
        if (/\d%/.test(d.value)) offenders.push(`${relative(ROOT, file)}:${d.line} ${d.selector} { ${d.prop}: ${d.value} }`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("the shell holds one contract for all three: own width, the viewport's cap, right-aligned, scrolls inside", () => {
    const decls = declarations(cssOnly(join(ROOT, "app/shell.css")));
    const shared = decls.filter((d) => d.selector.includes(".bx-topbar") && PANELS.every((p) => d.selector.includes(p)));
    const get = (prop: string) => shared.filter((d) => d.prop === prop).map((d) => d.value);
    expect(get("width")).toContain("min(var(--bx-pop-w), calc(100vw - 2 * var(--bx-pop-gutter)))");
    expect(get("max-width")).toContain("calc(100vw - 2 * var(--bx-pop-gutter))");
    expect(get("right")).toContain("0");
    expect(get("left")).toContain("auto");
    expect(get("overflow-y")).toContain("auto");
    expect(get("max-height").some((v) => v.includes("100dvh"))).toBe(true);
    // On a phone the panel pins to the viewport's right gutter rather than the trigger.
    expect(get("position")).toEqual(expect.arrayContaining(["absolute", "fixed"]));
    // Each panel names its own width.
    for (const p of PANELS) {
      const own = decls.find((d) => d.selector === `.bx-shell .bx-topbar ${p}` && d.prop === "--bx-pop-w");
      expect(own, p).toBeTruthy();
      expect(parseInt(own!.value, 10)).toBeGreaterThanOrEqual(260);
    }
  });

  it("Find a build lays out its own rows: names shrink to an ellipsis, the tabs wrap inside", () => {
    const decls = declarations(cssOnly(join(ROOT, "app/shell.css")));
    const rule = (sel: string) => Object.fromEntries(decls.filter((d) => d.selector === sel).map((d) => [d.prop, d.value]));
    const name = rule(".bx-shell .bx-topbar .bx-project-picker .bx-strip-name");
    expect(name).toMatchObject({ "min-width": "0", "text-overflow": "ellipsis", overflow: "hidden", "white-space": "nowrap" });
    expect(rule(".bx-shell .bx-topbar .bx-project-picker .bx-strip-filters")["flex-wrap"]).toBe("wrap");
    expect(rule(".bx-shell .bx-topbar .bx-project-picker #boss-project-search")).toMatchObject({ width: "100%", "min-width": "0" });
  });

  it("the bell's count is a corner badge, half the bell's height, in the label size", () => {
    const decls = declarations(cssOnly(join(ROOT, "app/shell.css"))).filter((d) => d.selector === ".bx-bell-n");
    const v = Object.fromEntries(decls.map((d) => [d.prop, d.value]));
    expect(v.height).toBe("16px");
    expect(v["font-size"]).toBe("var(--fs-label)");
  });
});

describe("Find a build renders its controls inside its panel", () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it("the search box, the tabs and every row are the panel's own children, each row titled in full", async () => {
    const long = "Shopfront analytics dashboard with a very long name that must truncate";
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (String(url).includes("/api/projects-strip")) {
        return new Response(JSON.stringify({ total: 2, projects: [
          { id: "p1", title: long, status: "DONE", stage: "Shipped", pct: 100, latest_run_id: "r1" },
          { id: "p2", title: "Shift scheduler", status: "GATED", stage: "Review", pct: 72, latest_run_id: "r2" },
        ] }), { headers: { "content-type": "application/json" } });
      }
      return new Response("{}", { status: 404 });
    }));
    const { ProjectStrip } = await import("../app/project-strip");
    await render(<div className="bx-shell"><header className="bx-topbar"><ProjectStrip projectId="proj_demo" area="overview" chip /></header></div>);
    await click("[data-testid=bx-strip-trigger]");
    const panel = $("[data-testid=bx-project-picker]");
    expect(panel).toBeTruthy();
    // Nothing sizes the panel from the trigger at runtime either.
    expect(panel!.getAttribute("style")).toBeNull();
    expect(panel!.contains($("#boss-project-search"))).toBe(true);
    const tabs = $$("[data-testid^=bx-strip-filter-]");
    expect(tabs.length).toBe(4);
    for (const t of tabs) expect(panel!.contains(t)).toBe(true);
    const rows = $$(".bx-strip-item");
    expect(rows.length).toBe(2);
    for (const r of rows) expect(panel!.contains(r)).toBe(true);
    // The row's tooltip carries the whole name, so a row that ends in an ellipsis is still readable.
    for (const r of rows) {
      const shown = r.querySelector(".bx-strip-name")!.textContent!;
      expect(r.getAttribute("title")).toContain(shown);
    }
  });
});
