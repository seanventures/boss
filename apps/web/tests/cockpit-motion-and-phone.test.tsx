// @vitest-environment happy-dom
/**
 * The cockpit's motion has a still, and it works at 390px (factory spec §5.2, §5.3, §7).
 *
 * Motion: no row animates at first paint; quiet, budget-wait and blocked builds hold every face
 * still (motion would claim work); the console's Paused stops it too; a hollow (next) face is
 * never ringed. Phone: the vertical station list carries every station with its word and an
 * aria label (`Build, Forge, coding`); Steer and Stop move into ⋯; the CSS folds the Line away and
 * makes touch targets 44px.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { $, $$, click, mount, unmount } from "./_helpers/live-render";
import { stubFetch } from "./_helpers/cockpit-render";
import { cockpit, cockpitPrOpen } from "./_helpers/factory-fixtures";

vi.mock("../app/code-tasks-ui", () => ({ DeploymentLine: () => <span data-testid="deploy-line" /> }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { Cockpit } = await import("../app/factory/cockpit");
const view = () => <Cockpit projectId="p1" runId="crun_inv2" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />;
const CSS = readFileSync(resolve(__dirname, "../app/factory/cockpit.css"), "utf8");

afterEach(() => { unmount(); vi.unstubAllGlobals(); setConsoleLive(true); });

const moving = () => $$('[data-testid="fx-cockpit"] [data-state="working"], [data-testid="fx-cockpit"] [data-state="needs_you"]')
  .filter((el) => !el.classList.contains("fx-state"));

describe("motion has a still", () => {
  it("a live build: the working face moves, nothing rises in at first paint", async () => {
    stubFetch(cockpit());
    await mount(view());
    expect(moving().length).toBeGreaterThan(0);
    expect($$(".fx-row[data-enter]")).toHaveLength(0);
  });

  for (const state of ["quiet", "budget_wait", "blocked"]) {
    it(`${state}: nothing moves`, async () => {
      stubFetch(cockpit({ state: { state, word: "Waiting · budget", verb: "Waiting", station: "build", k: 2, n: 8, rounds: 0, preview: null } }));
      await mount(view());
      expect($('[data-testid="fx-cockpit"]')?.dataset.still).toBe("true");
      expect(moving()).toHaveLength(0);
    });
  }

  it("Paused: nothing moves, and it says Paused", async () => {
    stubFetch(cockpit());
    await mount(view());
    expect(moving().length).toBeGreaterThan(0);
    await act(async () => setConsoleLive(false));
    expect($('[data-testid="fx-cockpit"]')?.dataset.still).toBe("true");
    expect(moving()).toHaveLength(0);
    expect($$(".fx-flag").map((f) => f.textContent)).toContain("Paused");
  });

  it("a hollow next face is never ringed", async () => {
    stubFetch(cockpit());
    await mount(view());
    const hollow = $$('[data-face="hollow"]');
    expect(hollow.length).toBeGreaterThan(0);
    for (const h of hollow) expect(h.querySelector('[data-state="working"], [data-state="needs_you"]')).toBeNull();
  });

  it("machinery stations show the BOSS glyph, never an agent", async () => {
    stubFetch(cockpitPrOpen());
    await mount(view());
    for (const k of ["pr", "preview"]) {
      const bay = $(`.fx-bay[data-key="${k}"]`)!;
      expect(bay.querySelector('[data-face="agent"]')).toBeNull();
    }
  });

  it("the stylesheet stills every animation under reduced motion and when still", () => {
    expect(CSS).toMatch(/@media \(prefers-reduced-motion: reduce\)[\s\S]*\.fx-row\[data-enter\] \{ animation: none; \}/);
    expect(CSS).toMatch(/\.fx-cockpit\[data-still\] \.fx-row\[data-enter\] \{ animation: none; \}/);
  });
});

describe("at 390px", () => {
  it("the vertical station list carries every station, labelled for a screen reader", async () => {
    window.innerWidth = 390;
    stubFetch(cockpit());
    await mount(view());
    const items = $$('[data-testid="fx-stations-v"] .fx-vstation');
    expect(items.map((i) => i.querySelector(".fx-vstation-label")?.textContent)).toEqual(
      ["Plan", "Tests", "Build", "Check", "Review", "PR", "Preview", "Ship"]);
    expect(items[2].getAttribute("aria-label")).toMatch(/^Build, .+, coding$/);
    expect($('[data-testid="fx-cockpit"]')?.dataset.band).toBe("phone");
  });

  it("Steer and Stop are in ⋯", async () => {
    stubFetch(cockpit());
    await mount(view());
    await click($('[data-testid="fx-more"]'));
    expect($$('[data-testid="fx-menu"] .fx-phone-only').map((b) => b.textContent)).toEqual(["Steer", "Stop"]);
  });

  it("the stylesheet folds the Line, shows the list, keeps 44px targets and no page scroll", () => {
    const phone = CSS.slice(CSS.indexOf("@media (max-width: 639px)"));
    expect(phone).toMatch(/\.fx-line-wrap \{ display: none; \}/);
    expect(phone).toMatch(/\.fx-stations-v \{ display: flex;/);
    expect(phone).toMatch(/min-height: 44px/);
    expect(phone).toMatch(/\.fx-wide-only \{ display: none; \}/);
    expect(CSS).toMatch(/\.fx-line-wrap \{[^}]*overflow-x: auto/);
    expect(CSS).toMatch(/padding: 0 16px 64px/);
  });
});
