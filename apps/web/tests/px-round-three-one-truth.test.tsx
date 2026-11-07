// @vitest-environment happy-dom
/**
 * ROUND 3 OF THE OPERATOR'S WALK (BOSSAPP-913/914, feat/px-r3): "every single piece in wireframes …
 * with utmost quality and clearly".
 *
 * 1. Overview › This week painted faded, "like a disabled card": every card's entrance started at
 *    opacity 0, and a card still off screen (the last one) was captured see-through. Entrances
 *    move, they never fade. Five "Today"s say their times instead.
 * 2. A Roadmap card is evidence → detector → agent from the engine's `card`, never the provenance
 *    chain "Code graph → Roadmap → Onboarding".
 * 5. Observability's filters are separate pills that scroll on a phone; its rows keep their actions
 *    on their line; its head and cards read at the shared type scale (globals --fs-*).
 * 6. "Builds 9 ⌄" is a chip in the top bar, not a row under it.
 */
import React from "react";
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";

import { timelineLabels } from "../app/projects/kit";
import { recFlow } from "../app/projects/tab-roadmap";
import { ProjectStrip } from "../app/project-strip";
import { parseRoadmap } from "../lib/ambient";
import { cssOnly, codeOnly } from "./_helpers/source";
import { $, click, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("This week (screen 04)", () => {
  const now = new Date(2026, 10, 6, 18, 0);
  const at = (d: number, h: number, m = 0) => new Date(2026, 10, d, h, m).toISOString();

  it("says each point's time when every point happened today", () => {
    const got = timelineLabels([at(6, 9, 5), at(6, 11, 40), at(6, 15, 0)], now);
    expect(got).toHaveLength(3);
    for (const label of got) expect(label).toMatch(/\d{1,2}[:.]\d{2}/);
    expect(new Set(got).size).toBe(3);
    expect(got).not.toContain("Today");
  });

  it("says the day when the points span days", () => {
    const got = timelineLabels([at(3, 9), at(5, 10), at(6, 9), null], now);
    expect(got[1]).toBe("Yesterday");
    expect(got[2]).toBe("Today");
    expect(got[3]).toBe("");
    expect(got[0]).toBe(new Date(2026, 10, 3).toLocaleDateString(undefined, { weekday: "short" }));
  });

  it("a card's entrance never starts see-through (the faded This week card)", () => {
    const px = cssOnly("app/projects/px.css");
    const rise = px.match(/@keyframes px-rise\s*\{([\s\S]*?\})\s*\}/)![1];
    expect(rise).not.toMatch(/opacity/);
    expect(px).toMatch(/\.px-card \{[^}]*animation: px-rise/);
    const obs = cssOnly("app/observability/obs.css");
    expect(obs.match(/@keyframes obs-arow-in\s*\{([\s\S]*?\})\s*\}/)![1]).not.toMatch(/opacity/);
  });
});

describe("Roadmap cards (screen 07)", () => {
  const view = parseRoadmap({ status: "open", counts: { open: 1 }, items: [{
    id: "wi_1", title: "Test service.mjs", kind: "test_gap", detector: "onboarding", status: "open", rank: 1,
    squad: ["qa"], badges: { kind: "Test gap", cost_usd: 21.64 },
    strip: [{ type: "signal", label: "Code graph", badges: ["×1"] }, { type: "signal", label: "Roadmap", badges: [] },
            { type: "agent", label: "Onboarding", badges: [], icon: "compass" }],
    card: { evidence: [{ label: "service.mjs hot", icon: "fire" }, { label: "No tests", icon: "test-tube" }],
            detector: { label: "Test gap", icon: "test-tube" }, does: "Add tests", cost_usd: 21.64,
            cost_basis: "a ticket here, from past runs" },
  }] });

  it("draws the engine's evidence → detector → agent, never the provenance chain", () => {
    const rec = view.recommendations[0];
    expect(rec.card?.does).toBe("Add tests");
    const nodes = recFlow(rec, [{ name: "Vera", role_key: "qa" }]);
    expect(nodes.map((n) => n.label)).toEqual(["service.mjs hot", "No tests", "Test gap", "Add tests"]);
    expect(nodes.map((n) => n.type)).toEqual(["signal", "signal", "decision", "agent"]);
    expect(nodes.map((n) => n.label).join(" ")).not.toMatch(/Code graph|Roadmap|Onboarding/);
    expect((nodes[3] as { title?: string }).title).toBe("Vera would do it");
  });

  it("an older engine without `card` still reads its strip", () => {
    const old = parseRoadmap({ status: "open", counts: {}, items: [{ id: "wi_2", title: "x", detector: "incident_cause", kind: "hardening",
      strip: [{ type: "signal", label: "Incidents", badges: ["×2"] }, { type: "agent", label: "Incident hotspot", badges: [] }] }] });
    expect(old.recommendations[0].card).toBeNull();
    expect(recFlow(old.recommendations[0]).map((n) => n.label)).toEqual(["2 incidents", "Incident hotspot", "Test + fix"]);
  });
});

describe("Observability at the shared scale (screen 15)", () => {
  const obs = cssOnly("app/observability/obs.css");
  const inf = cssOnly("app/infra/infra.css");
  const globals = cssOnly("app/globals.css");

  it("the type scale is named once", () => {
    for (const t of ["--fs-page", "--fs-title", "--fs-body", "--fs-row", "--fs-ui", "--fs-meta", "--fs-tag", "--fs-label"]) {
      expect(globals).toMatch(new RegExp(`${t}:\\s*\\.?\\d`));
    }
  });

  it("the head, counts, Find and attention cards use it", () => {
    expect(inf).toMatch(/\.inf-head-counts \{[^}]*font-size: var\(--fs-meta\)/);
    expect(inf).toMatch(/\.inf-find input \{[^}]*font-size: var\(--fs-ui\)/);
    expect(inf).toMatch(/\.inf-tab \{[^}]*font-size: var\(--fs-ui\)/);
    expect(globals).toMatch(/:is\(\.inf-needs, \.fx\) \.ag-att-title \{[^}]*font-size: var\(--fs-body\)/);
    // No size of the old, larger scale is left in either sheet.
    for (const sheet of [obs, inf]) {
      const big = [...sheet.matchAll(/font-size:\s*0?\.(\d+)rem/g)].map((m) => Number(`0.${m[1]}`)).filter((v) => v >= 0.72 && v < 0.95);
      expect(big).toEqual([]);
    }
  });

  it("on a phone the filters scroll in one row with a fade, and a row keeps its actions inline", () => {
    const phone = obs.slice(obs.lastIndexOf("@media (max-width: 639px)"));
    expect(phone).toMatch(/\.obs \.inf-tabs \{[^}]*flex-wrap: nowrap;[^}]*overflow-x: auto/);
    expect(phone).toMatch(/mask-image: linear-gradient/);
    expect(obs).toMatch(/\.obs \.inf-tabs \{[^}]*background: none/);
    const narrow = obs.slice(obs.indexOf("@media (max-width: 760px)"));
    expect(narrow).toMatch(/\.obs-arow-links \{ grid-column: auto;/);
  });
});

describe("Builds, a chip in the top bar", () => {
  it("renders inside the bar, beside the bell, and nowhere under it", () => {
    const shell = codeOnly("app/ConsoleShell.tsx");
    const bar = shell.slice(shell.indexOf('<header className="bx-topbar">'), shell.indexOf("</header>"));
    expect(bar).toMatch(/<ProjectStrip [^>]*chip \/>/);
    expect(bar.indexOf("<ProjectStrip")).toBeLessThan(bar.indexOf('<PresencePill'));
    expect(shell.split("<ProjectStrip").length).toBe(2);
    expect(shell).toContain("id={STRIP_NOTICE_SLOT}");
  });

  it("is a compact trigger that opens the same list", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ total: 2, projects: [
      { id: "a", title: "Ticket search", status: "GATED", stage: "Review", pct: 60, latest_run_id: "r1" },
      { id: "b", title: "Export", status: "RUNNING", stage: "Build", pct: 30, latest_run_id: "r2" }] })));
    await mount(<ProjectStrip projectId="p1" chip />);
    await settle();
    const nav = $("[data-testid=bx-strip]")!;
    expect(nav.className).toContain("bx-strip-chip");
    const trigger = $("[data-testid=bx-strip-trigger]")!;
    expect(trigger.textContent).toContain("Builds");
    expect(trigger.textContent).toContain("2");
    expect(trigger.textContent).toContain("1 need attention");
    expect(trigger.getAttribute("title")).toBe("2 builds · 1 need attention — open the list");
    await click(trigger);
    expect($("[data-testid=bx-strip-a]")?.textContent).toContain("Ticket search");
  });

  it("is styled as a chip, with no row of its own", () => {
    const shell = cssOnly("app/shell.css");
    expect(shell).toMatch(/\.bx-shell \.bx-topbar > \.bx-strip-chip \{[^}]*border: 0/);
    expect(shell).toMatch(/\.bx-strip-chip > \.bx-project-trigger[^{]*\{[^}]*height: 32px/);
    expect(readFileSync("app/strip-notice.ts", "utf8")).toContain("export const STRIP_NOTICE_SLOT");
  });
});
