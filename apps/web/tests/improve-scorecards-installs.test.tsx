// @vitest-environment happy-dom
/**
 * Observability › Scorecards, Experiments and Installs (docs/product/RUN_SCORECARDS.md): the
 * server's numbers, rows that open the run, "Too close to call", one-press Promote, and an
 * Installs tab that exists only in Collector mode.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { fakeImproveApi, newImprove } from "./_helpers/improve-fixtures";
import { mockObsFetch } from "./_helpers/obs-fixtures";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { ObsHome } = await import("../app/observability/obs-home");

async function open(url: string, collector = false) {
  const { fn } = mockObsFetch();
  const f = newImprove();
  f.sharing.switches.collector.on = collector;
  vi.stubGlobal("fetch", fakeImproveApi(f, fn as never));
  window.history.replaceState(null, "", `/?area=observability&${url}`);
  await mount(<ObsHome projectId="p1" actor="ceo" />);
  await settle();
  return f;
}

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("Observability › Scorecards", () => {
  it("draws the server's groups and what's costing you; rows open the run", async () => {
    const f = await open("view=scorecards");
    expect($$("[data-testid=sc-row]")).toHaveLength(2);
    expect($("[data-testid=sc-tiles]")!.textContent).toContain("0.71");
    const links = $$("[data-testid=sc-cost-row] a").map((a) => a.getAttribute("href"));
    expect(links).toEqual(["/?area=code_tasks&build=crun_9", "/?area=code_tasks&build=crun_7"]);
    expect($("[data-testid=obs-window]")).toBeNull(); // its own 7d · 30d · 90d instead
    await click($("[data-testid=sc-by-harness]"));
    await settle();
    expect(f.calls.some((c) => c.view === "scorecards" && c.url.includes("group=harness"))).toBe(true);
    expect(window.location.search).toContain("by=harness");
  });

  it("experiments say too close to call, and Promote is one press", async () => {
    const f = await open("view=scorecards&sc=experiments");
    expect($("[data-testid=sc-exp-verdict]")!.textContent).toBe("Too close to call");
    expect($("[data-testid=sc-arm-a]")!.textContent).toContain("Claude Code");
    expect(document.body.textContent).not.toMatch(/claude_code|software_engineer/);
    await click($("[data-testid=sc-promote-b]"));
    await settle();
    // A confirm sheet first: what changes, and short evidence needs the operator's own tick.
    expect(f.calls.some((c) => c.op === "experiment_promote")).toBe(false);
    expect($("[data-testid=sc-promote-change]")!.textContent).toContain("Claude Code → Codex");
    expect(($("[data-testid=sc-promote-yes]") as HTMLButtonElement).disabled).toBe(true);
    await click($("[data-testid=sc-promote-anyway]"));
    await click($("[data-testid=sc-promote-yes]"));
    await settle();
    expect(f.calls.find((c) => c.op === "experiment_promote")?.body).toMatchObject({ id: "exp_1", arm: "b", anyway: true });
  });

  it("a new experiment posts exactly what the form says", async () => {
    const f = await open("view=scorecards&sc=experiments");
    await click($("[data-testid=sc-exp-add]"));
    expect(($("[data-testid=sc-new-key]") as HTMLElement).tagName).toBe("SELECT");  // a picker, not free text
    expect($("[data-testid=sc-new-key]")!.textContent).toContain("Software Engineer");
    await click($("[data-testid=sc-new-start]"));
    await settle();
    expect(f.calls.find((c) => c.op === "experiment")?.body).toMatchObject({
      target_kind: "role", target_key: "software_engineer", knob: "harness", arm_a: "claude_code", arm_b: "codex",
      split_b: 0.5, target_units: 20, arm_cost_cap_usd: 25 });
  });
});

describe("Observability › Installs", () => {
  it("is not a tab unless Collector mode is on", async () => {
    await open("view=apps", false);
    expect($("[data-testid=obs-tab-installs], button[role=tab][data-key=installs]")).toBeNull();
    expect(document.body.textContent).not.toContain("Installs");
  });

  it("leads with the proof and exports CSV", async () => {
    await open("view=installs", true);
    expect($("[data-testid=in-proof]")!.textContent).toContain("312 active installs this month, 41% still active after 30 days");
    expect($("[data-testid=in-active]")!.textContent).toContain("312");
    expect($("[data-testid=in-csv]")!.getAttribute("href")).toContain("view=collector_csv");
    expect($("[data-testid=in-harness]")!.textContent).toContain("claude_code · 0.74");
    expect($("[data-testid=in-installer]")!.textContent).toContain("Not reported");
  });
});

describe("Installs on a phone", () => {
  it("the retention table scrolls inside its card", async () => {
    await open("view=installs", true);
    expect($("[data-testid=in-cohorts-scroll] table")).toBeTruthy();
  });
});
