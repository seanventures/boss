// @vitest-environment happy-dom
/**
 * Factory › Apps on shared-server (26 real apps): names cut mid-word, rows misaligned (some had a
 * chevron, some not), crews smudged, every build "Attempt 1", empty middles, a crammed foot.
 * Apps is now one aligned table, drawn here from a fixture with the REAL shape (sanitised from
 * the live /v1/factory/apps: 25 apps, 2–4 builds, crews of 4–5).
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import realShape from "./_helpers/apps-real-shape.json";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";
import { visibleRuns } from "./_helpers/cockpit-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { AppsTab, parseApps, sortApps } = await import("../app/factory/apps-tab");

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

async function render(body: unknown = realShape, onOpen: (id: string) => void = () => {}) {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json(body)));
  await mount(<AppsTab projectId="p1" runs={[]} previews={[]} query="" onOpen={(id) => onOpen(id)} onNew={() => {}} />);
  await settle();
}

describe("Apps is an aligned table", () => {
  it("every row has the same columns, a chevron cell first (disabled for one build)", async () => {
    await render();
    const rows = $$(".fx-app-row");
    expect(rows.length).toBe((realShape as { apps: unknown[] }).apps.length);
    for (const row of rows) {
      const cells = [...row.children].map((c) => [...c.classList].find((k) => k.startsWith("fx-c-")));
      expect(cells).toEqual(["fx-c-chev", "fx-c-app", "fx-c-crew", "fx-c-state", "fx-c-pr", "fx-c-deploy",
                             "fx-c-spend", "fx-c-builds", "fx-c-updated"]);
      const chev = row.querySelector<HTMLButtonElement>(".fx-app-chev")!;
      expect(chev).not.toBeNull();
      const builds = Number(row.querySelector(".fx-c-builds")?.textContent);
      expect(chev.disabled).toBe(builds <= 1);
    }
    const head = [...$(".fx-apps-head")!.children].map((c) => [...c.classList].find((k) => k.startsWith("fx-c-")));
    expect(head).toEqual(["fx-c-chev", "fx-c-app", "fx-c-crew", "fx-c-state", "fx-c-pr", "fx-c-deploy",
                          "fx-c-spend", "fx-c-builds", "fx-c-updated"]);
  });

  it("names are whole (no mid-word cut), crews are separate faces (max 5 + n)", async () => {
    await render();
    for (const name of $$(".fx-app-name")) expect(name.textContent).not.toMatch(/…$/);
    expect($$(".fx-app-name").map((n) => n.textContent)).toContain("Unified SaaS License-Provisioning Flow");
    for (const crew of $$(".fx-crew")) {
      expect(crew.querySelectorAll(".fx-crew-face").length).toBeLessThanOrEqual(5);
      for (const f of crew.querySelectorAll(".fx-crew-face")) expect(f.getAttribute("title")).toBeTruthy();
    }
    expect($(".ag-stack")).toBeNull();
  });

  it("the whole row opens the build; no ↗ on every row", async () => {
    const opened: string[] = [];
    await render(realShape, (id) => opened.push(id));
    await click($$(".fx-app-row")[0].querySelector(".fx-c-state"));
    expect(opened.length).toBe(1);
    expect($$(".fx-app-row").some((r) => (r.textContent ?? "").includes("↗"))).toBe(false);
  });

  it("builds are numbered by their place in the app, newest marked latest", async () => {
    await render();
    const row = $$(".fx-app-row").find((r) => Number(r.querySelector(".fx-c-builds")?.textContent) >= 3)!;
    await click(row.querySelector(".fx-app-chev"));
    const nums = $$('[data-testid^="fx-build-n-"]').map((n) => n.textContent);
    const n = nums.length;
    expect(nums).toEqual(Array.from({ length: n }, (_, i) => `Build ${n - i}`));
    expect($$(".fx-app-build")[0].querySelector(".fx-latest")?.textContent).toBe("latest");
    // No ring for a station a build never ran.
    for (const bay of $$(".fx-app-build .fx-bay")) {
      expect(["pending", "skipped"]).not.toContain(bay.getAttribute("data-status"));
    }
  });

  it("the server's order wins over attempt numbers", () => {
    const apps = parseApps({ apps: [{ app: "X", run_id: "r2", attempts: [
      { run_id: "r2", attempt: 1, order: 2, word: "PR open", pr_number: 9, pr_state: "merged" },
      { run_id: "r1", attempt: 1, order: 1, word: "Failed" }] }] });
    expect(apps[0].builds.map((b) => b.order)).toEqual([2, 1]);
    expect(apps[0].builds[0].prState).toBe("merged");
  });

  it("PR says its state; spend is metered or Included; sorting by Spend and Name", async () => {
    const body = { apps: [
      { app: "Beta", run_id: "rb", state: "pr_open", word: "PR open", builders: [], deploys: [], attempts: [],
        pr_number: 7, pr_url: "https://github.example/a/b/pull/7", pr_state: "merged", cost_usd: 3.82, at: "2026-10-01T00:00:00Z" },
      { app: "Alpha", run_id: "ra", state: "pr_open", word: "PR open", builders: [], deploys: [], attempts: [],
        cost_usd: 0, included: true, at: "2026-10-03T00:00:00Z" },
    ] };
    await render(body);
    expect($$(".fx-app-name").map((n) => n.textContent)).toEqual(["Alpha", "Beta"]);
    expect($('[data-testid="fx-app-pr-rb"]')?.textContent).toBe("#7");
    expect($$(".fx-app-row")[1].querySelector(".fx-pr-state")?.textContent).toBe("merged");
    expect($$(".fx-app-spend").map((s) => s.textContent)).toEqual(["Included", "$3.82"]);
    await click($('[data-testid="fx-apps-sort-spend"]'));
    expect($$(".fx-app-name").map((n) => n.textContent)).toEqual(["Beta", "Alpha"]);
    await click($('[data-testid="fx-apps-sort-name"]'));
    expect($$(".fx-app-name").map((n) => n.textContent)).toEqual(["Alpha", "Beta"]);
    expect(sortApps(parseApps(body), "updated").map((a) => a.app)).toEqual(["Alpha", "Beta"]);
  });

  it("keeps to the copy budget: no run over 6 words, no <p>, no banned words", async () => {
    await render();
    await click($$(".fx-app-chev:not([disabled])")[0]);
    const root = $('[data-testid="fx-apps"]')!;
    expect(root.querySelectorAll("p").length).toBe(0);
    const runs = visibleRuns(root);
    expect(runs.filter((r) => r.split(/\s+/).length > 6)).toEqual([]);
    expect(runs.filter((r) => /\b(project|code task|pipeline|ETA|estimated)\b|%/i.test(r))).toEqual([]);
  });
});

describe("the State column", () => {
  it("says the station only while the build is on the line", async () => {
    const { buildRow } = await import("./_helpers/factory-fixtures");
    const live = { ...buildRow({ run_id: "r_live", state: "building", word: "Reviewing", station: "review" }), at: "2026-10-04T09:00:00Z" };
    const done = { ...buildRow({ run_id: "r_done", state: "pr_open", word: "PR open", station: "build" }), at: "2026-10-04T09:00:00Z" };
    await render({ apps: [
      { app: "On line", run_id: "r_live", state: "building", word: "Reviewing", builders: [], deploys: [], attempts: [live] },
      { app: "Shipped", run_id: "r_done", state: "pr_open", word: "PR open", builders: [], deploys: [], attempts: [done] },
    ] });
    const states = Object.fromEntries($$(".fx-app-row").map((r) => [r.querySelector(".fx-app-name")?.textContent,
      (r.querySelector(".fx-c-state")?.textContent ?? "").trim()]));
    expect(states).toEqual({ "On line": "Reviewing · Review", Shipped: "PR open" });
  });
});

describe("adding a feature is on every app's row", () => {
  it("a hover action and the row's menu both open New project on that app's working build", async () => {
    const opened: string[] = [];
    await render(realShape, (id) => opened.push(id));
    const first = (realShape as { apps: Array<{ key?: string; run_id: string }> }).apps[0];
    const key = first.run_id;
    const add = $(`[data-testid="fx-app-add-${key}"]`) as HTMLAnchorElement;
    const href = new URLSearchParams(add.getAttribute("href")!.replace(/^\?/, ""));
    expect([href.get("area"), href.get("compose")]).toEqual(["delivery_workflows", "add"]);
    expect(href.get("app")).toMatch(/^crun_/);
    await click($(`[data-testid="fx-app-more-${key}"]`));
    expect($(`[data-testid="fx-app-menu-add-${key}"]`)?.getAttribute("href")).toBe(add.getAttribute("href"));
    // Neither press opens the build underneath it.
    expect(opened).toEqual([]);
  });
});
