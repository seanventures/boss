// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * WORKFLOWS: RUNS · LIBRARY, AND ONE WIZARD (BOSSAPP-989). The "New build" tab's screens (new
 * app · add a feature) are gone: the tab row's "+ New build" and every old door — `tab=new`,
 * `?new=1`, `?compose=new|template`, a `brief=` hand-over — open the one Add-project wizard
 * (`?new_project=idea`), and `?compose=add[&app=]` lands on Project › New work. What remains
 * here is the workflow START FORM: Library › Start opens the composer on that workflow, not a
 * tab and not in the URL; a Start lands on Runs with the new run highlighted.
 *
 * The composer is a stand-in here (its own behaviour is tests/new-project-compose.test.tsx):
 * this is about where the area puts it and where it sends you after.
 */
vi.mock("../app/workflows/studio-parts", () => import("./_helpers/studio-parts-stub"));
vi.mock("../app/workflows/new-project-composer", () => ({
  NewProjectComposer: (p: { mode: string | null; appKey?: string | null; startWith?: string;
                            onStarted: (id: string | null) => void; onCancel?: () => void }) => (
    <div data-testid="np-stub" data-mode={p.mode ?? ""} data-app={p.appKey ?? ""}
         data-workflow={p.startWith ?? ""}>
      <button type="button" data-testid="np-stub-start" onClick={() => p.onStarted("wfr_2")}>Start</button>
      {p.onCancel ? <button type="button" data-testid="np-stub-cancel" onClick={p.onCancel}>Cancel</button> : null}
    </div>
  ),
}));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { DeliveryWorkflows, readAreaUrl, tabKeyTarget, namesATab, urlWithTab } = await import("../app/delivery-workflows-ui");
const { openNewProject } = await import("../lib/compose-links");
const stub = await import("./_helpers/studio-parts-stub");

const run = (id: string, title: string) => ({
  id, status: "running", workflow_key: "standard_product_delivery",
  workflow_name: "Standard Product Delivery", version: 3, step_count: 1, steps_done: 0,
  current_step: "build", waiting_on: null, blocked_reason: null, error: null, updated_at: null,
  input: { title }, finished: false, standing: "active", actions: [],
  steps: [{ step_id: "build", kind: "capability", status: "running", capability: "build", output: {},
            evidence: [], error: null, approval: {}, due_at: null, timeout_applied: null, attempt: 1,
            started_at: null, completed_at: null, decision_id: null }],
});

let runs: ReturnType<typeof run>[] = [];
const views = (): Record<string, unknown> => ({
  capabilities: { capabilities: [{ key: "build", label: "Build" }], manual_approval_risk_floor: 2 },
  catalogue: { items: [] },
  installed: { items: [] },
  runs: { runs },
  custom_steps: { steps: [] },
  schedules: { schedules: [] },
});

let root: Root | null = null;
let host: HTMLElement | null = null;
const scrolled: string[] = [];
const originalScroll = Element.prototype.scrollIntoView;

beforeEach(() => {
  runs = [run("wfr_1", "Shift swap board")];
  scrolled.length = 0;
  Element.prototype.scrollIntoView = function (this: Element) {
    scrolled.push((this as HTMLElement).dataset?.runId ?? "");
  };
  window.history.replaceState(null, "", "/?area=delivery_workflows&org=o1");
  stub.fetchLibrary.mockResolvedValue([]);
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    if ((init?.method ?? "GET").toUpperCase() === "GET") {
      return Response.json(views()[url.searchParams.get("view") ?? ""] ?? {});
    }
    return Response.json({ ok: true });
  }));
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  Element.prototype.scrollIntoView = originalScroll;
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

async function settle() {
  for (let i = 0; i < 20; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
async function mount() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<DeliveryWorkflows projectId="p1" actor="ceo" />));
  await settle();
}
const $ = (sel: string) => host!.querySelector<HTMLElement>(sel);
const params = () => new URLSearchParams(window.location.search);
const selected = () => host!.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.dataset.testid;
const click = async (el: HTMLElement | null) => {
  expect(el).not.toBeNull();
  await act(async () => { el!.click(); });
  await settle();
};
const key = async (el: HTMLElement | null, k: string) => {
  await act(async () => { el!.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true })); });
  await settle();
};
const navigateTo = async (search: string) => {
  await act(async () => {
    window.history.pushState(null, "", `/${search}`);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await settle();
};

describe("two tabs: Runs · Library, and the + New build door", () => {
  it("the tablist is Runs (counting) and Library, opening on Runs", async () => {
    await mount();
    const tabs = [...host!.querySelectorAll<HTMLElement>('[role="tablist"][aria-label="Workflows"] [role="tab"]')];
    expect(tabs.map((t) => t.textContent)).toEqual(["Runs (1)", "Library"]);
    expect(selected()).toBe("dw-tab-runs");
    // Runs is only the list: no composer in it, and no button that would stack one over it.
    expect($('[data-testid="dw-panel-runs"]')).not.toBeNull();
    expect($('[data-testid="np-stub"]')).toBeNull();
    expect(host!.querySelectorAll('[data-testid="dw-run"]')).toHaveLength(1);
  });

  it("+ New build is not a tab: it opens the one Add-project wizard and leaves the tab alone", async () => {
    await mount();
    const door = $('[data-testid="dw-tab-new"]');
    expect(door?.getAttribute("role")).not.toBe("tab");
    await click(door);
    expect(selected()).toBe("dw-tab-runs");
    expect(params().get("new_project")).toBe("idea");
    // No composer: the wizard (start-host) draws over the area.
    expect($('[data-testid="np-stub"]')).toBeNull();
  });

  it("a tab is a tabpanel's label (aria-controls / aria-labelledby)", async () => {
    await mount();
    expect($('[data-testid="dw-tab-runs"]')?.getAttribute("aria-controls")).toBe("dw-panel-runs");
    expect($('[data-testid="dw-panel-runs"]')?.getAttribute("aria-labelledby")).toBe("dw-tab-runs");
  });
});

describe("Library › Start opens the workflow's start form; Start lands on Runs", () => {
  const library = () => stub.fetchLibrary.mockResolvedValue([
    { definition_id: "def_v", workflow_key: "family_video", name: "Family video", is_catalogue: false,
      version: 2, step_count: 1, run_count: 0, steps: [{ id: "fetch", title: "Fetch" }] },
  ]);

  it("a Library card's Start opens the form on that workflow, not a tab, not in the URL", async () => {
    library();
    window.history.replaceState(null, "", "/?area=delivery_workflows&org=o1&tab=library");
    await mount();
    await click($('[data-testid="wf-card-start-family_video"]'));
    expect($('[data-testid="np-stub"]')?.dataset.workflow).toBe("family_video");
    // Never the deprecated two-job screen: the form is the chosen workflow's, mode fixed.
    expect($('[data-testid="np-stub"]')?.dataset.mode).toBe("new");
    expect(params().get("tab")).toBe("library");
  });

  it("Start switches to Runs, opens the run, highlights it and scrolls it into view", async () => {
    library();
    window.history.replaceState(null, "", "/?area=delivery_workflows&org=o1&tab=library");
    await mount();
    await click($('[data-testid="wf-card-start-family_video"]'));
    runs = [run("wfr_1", "Shift swap board"), run("wfr_2", "Invoice chaser")];
    await click($('[data-testid="np-stub-start"]'));
    expect(selected()).toBe("dw-tab-runs");
    expect(params().get("tab")).toBe("runs");
    expect(params().get("wfr")).toBe("wfr_2");
    const card = $('[data-run-id="wfr_2"]');
    expect(card?.dataset.justStarted).toBe("true");
    expect(card?.dataset.open).toBe("true");
    expect($('[data-run-id="wfr_1"]')?.dataset.justStarted).toBeUndefined();
    expect(scrolled).toEqual(["wfr_2"]);
    expect($('[data-testid="np-stub"]')).toBeNull();
  });

  it("Cancel leaves the form and returns to the tab it came from", async () => {
    library();
    window.history.replaceState(null, "", "/?area=delivery_workflows&org=o1&tab=library");
    await mount();
    await click($('[data-testid="wf-card-start-family_video"]'));
    await click($('[data-testid="np-stub-cancel"]'));
    expect($('[data-testid="np-stub"]')).toBeNull();
    expect(selected()).toBe("dw-tab-library");
  });
});

describe("every old New build door lands in the one wizard or on New work (BOSSAPP-989)", () => {
  it.each([
    ["the legacy openNewProject door (compose=new)", "?area=delivery_workflows&org=o1&compose=new"],
    ["the Factory menu's legacy ?new=1", "?area=delivery_workflows&org=o1&new=1"],
    ["an old compose=template link", "?area=delivery_workflows&org=o1&compose=template"],
  ])("%s opens Add a project", async (_door, search) => {
    window.history.replaceState(null, "", `/${search}`);
    await mount();
    // No composer screen: the command is consumed and the wizard asked for instead.
    expect($('[data-testid="np-stub"]')).toBeNull();
    expect(params().get("new_project")).toBe("idea");
    expect([params().get("compose"), params().get("new")]).toEqual([null, null]);
    expect(params().get("org")).toBe("o1");
  });

  it("Add a feature (compose=add&app=) lands on Project › New work with the app named", async () => {
    window.history.replaceState(null, "", "/?area=delivery_workflows&org=o1&compose=add&app=helpdesk");
    await mount();
    expect($('[data-testid="np-stub"]')).toBeNull();
    expect([params().get("area"), params().get("tab"), params().get("compose"), params().get("app")])
      .toEqual(["project", "work", "1", "helpdesk"]);
    expect(params().get("org")).toBe("o1");
  });

  it("an old Build-it hand-over (brief= and title=) opens the wizard with both kept", async () => {
    window.history.replaceState(null, "", "/?area=delivery_workflows&org=o1&brief=A+helpdesk&title=HelpDesk");
    await mount();
    expect($('[data-testid="np-stub"]')).toBeNull();
    expect(params().get("new_project")).toBe("idea");
    expect(params().get("brief")).toBe("A helpdesk");
    expect(params().get("title")).toBe("HelpDesk");
  });

  it("while on Runs, openNewProject opens Add a project and leaves the tab alone (v2)", async () => {
    await mount();
    expect(selected()).toBe("dw-tab-runs");
    await act(async () => { openNewProject(); });
    await settle();
    expect(selected()).toBe("dw-tab-runs");
    expect(params().get("new_project")).toBe("idea");
  });
});

describe("the tab is in the URL", () => {
  it("an old tab=new link opens the wizard and the URL lands on Runs, honestly", async () => {
    window.history.replaceState(null, "", "/?area=delivery_workflows&org=o1&tab=new");
    await mount();
    expect(selected()).toBe("dw-tab-runs");
    expect($('[data-testid="np-stub"]')).toBeNull();
    expect(params().get("tab")).toBe("runs");
    expect(params().get("new_project")).toBe("idea");
  });

  it("Back and Forward move between the tabs", async () => {
    await mount();
    await click($('[data-testid="dw-tab-library"]'));
    expect(selected()).toBe("dw-tab-library");
    await navigateTo("?area=delivery_workflows&org=o1&tab=runs");
    expect(selected()).toBe("dw-tab-runs");
    await navigateTo("?area=delivery_workflows&org=o1&tab=library");
    expect(selected()).toBe("dw-tab-library");
  });

  it("a link to a run opens Runs even when the URL still said tab=new, and the URL says so", async () => {
    window.history.replaceState(null, "", "/?area=delivery_workflows&tab=new&wfr=wfr_1");
    await mount();
    expect(selected()).toBe("dw-tab-runs");
    expect($('[data-run-id="wfr_1"]')?.dataset.open).toBe("true");
    // rc10.5 m4: the stale tab=new is corrected in place, so a reload or a copied link is honest.
    expect([params().get("tab"), params().get("wfr")]).toEqual(["runs", "wfr_1"]);
  });

  it("urlWithTab corrects only a tab that disagrees", () => {
    expect(urlWithTab("?area=delivery_workflows&tab=new&wfr=w", "runs")).toBe("?area=delivery_workflows&tab=runs&wfr=w");
    expect(urlWithTab("?area=delivery_workflows&tab=runs", "runs")).toBeNull();
    expect(urlWithTab("?area=x", "runs")).toBeNull();
    // another area's tab is never this area's to correct (brec_4cafbcc6)
    expect(urlWithTab("?area=budget&tab=selffix", "runs")).toBeNull();
    expect(urlWithTab("?tab=new&wfr=w", "runs")).toBeNull();
  });

  it("tab=new is not a place any more: readAreaUrl never returns it", () => {
    expect(readAreaUrl("?tab=new")).toEqual({ tab: "runs", wf: null });
    expect(readAreaUrl("?tab=library").tab).toBe("library");
    expect(readAreaUrl("?tab=NEW").tab).toBe("runs");
    expect(namesATab("?tab=runs")).toBe(true);
    expect(namesATab("?area=delivery_workflows")).toBe(false);
  });
});

describe("an empty project", () => {
  it("stays on Runs, whose empty state opens the wizard", async () => {
    runs = [];
    await mount();
    expect(selected()).toBe("dw-tab-runs");
    await click($('[data-testid="dw-empty-new"]'));
    expect(selected()).toBe("dw-tab-runs");
    expect(params().get("new_project")).toBe("idea");
  });
});

describe("keyboard: one tab stop, arrows move and select", () => {
  it("only the selected tab is in the tab order", async () => {
    await mount();
    const order = ["dw-tab-runs", "dw-tab-library"].map((id) => $(`[data-testid="${id}"]`)?.tabIndex);
    expect(order).toEqual([0, -1]);
  });

  it("→ and ← move (wrapping), Home and End go to the ends", async () => {
    await mount();
    await key($('[data-testid="dw-tab-runs"]'), "ArrowRight");
    expect(selected()).toBe("dw-tab-library");
    expect(document.activeElement?.getAttribute("data-testid")).toBe("dw-tab-library");
    await key($('[data-testid="dw-tab-library"]'), "ArrowRight");
    expect(selected()).toBe("dw-tab-runs");
    await key($('[data-testid="dw-tab-runs"]'), "ArrowLeft");
    expect(selected()).toBe("dw-tab-library");
    await key($('[data-testid="dw-tab-library"]'), "Home");
    expect(selected()).toBe("dw-tab-runs");
    await key($('[data-testid="dw-tab-runs"]'), "End");
    expect(selected()).toBe("dw-tab-library");
  });

  it("the move, as a pure function", () => {
    expect([tabKeyTarget("ArrowRight", 1, 2), tabKeyTarget("ArrowLeft", 0, 2),
            tabKeyTarget("Home", 1, 2), tabKeyTarget("End", 0, 2), tabKeyTarget("a", 0, 2)])
      .toEqual([0, 1, 0, 1, -1]);
  });
});
