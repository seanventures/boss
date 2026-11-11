// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The Workflows area, mounted: three tabs synced to the URL, the editor full width on `&wf=`,
 * every Start landing on the New project tab preselected, and a run drawn on the lane with its
 * step's detail under it (Workflow Studio spec 2.1, 2.2, 3.6).
 *
 * The lane, the editor and the API client are stand-ins (tests/_helpers/studio-parts-stub.tsx):
 * this is about the area that mounts them — what it asks them to show and where it sends you.
 */
vi.mock("../app/workflows/studio-parts", () => import("./_helpers/studio-parts-stub"));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { DeliveryWorkflows } = await import("../app/delivery-workflows-ui");
const stub = await import("./_helpers/studio-parts-stub");

const step = (id: string, status: string, extra: Record<string, unknown> = {}) => ({
  step_id: id, kind: "capability", status, capability: id, output: {}, evidence: [],
  error: null, approval: {}, due_at: null, timeout_applied: null, attempt: 1,
  started_at: null, completed_at: null, decision_id: null, ...extra,
});

/** A run of eleven steps: the "fits in one row" case the lane is measured on. */
const ELEVEN = ["research", "panel", "prd", "spec", "build", "review", "test", "preview",
                "approve", "deploy", "monitor"];
const RUN = {
  id: "wfr_1", status: "running", workflow_key: "standard_product_delivery",
  workflow_name: "Standard Product Delivery", version: 3, step_count: 11, steps_done: 4,
  current_step: "build", waiting_on: null, blocked_reason: null, error: null, updated_at: null,
  input: { title: "Shift swap board" }, finished: false, standing: "active", actions: [],
  steps: ELEVEN.map((id, i) => step(id, i < 4 ? "succeeded" : i === 4 ? "running" : "pending")),
};

const VIEWS: Record<string, unknown> = {
  capabilities: { capabilities: ELEVEN.map((key) => ({ key, label: `Do ${key}` })),
                  manual_approval_risk_floor: 2 },
  catalogue: { items: [{ key: "standard_product_delivery", name: "Standard Product Delivery",
                         work_class: "project.new", steps: [], gates: [], step_count: 11 }] },
  installed: { items: [{ id: "def_v", workflow_key: "family_video", name: "Family video",
                         is_catalogue: false, forked_from_version_id: null, namespace: "product",
                         active_version: 2,
                         input_schema: { required: ["video_url"],
                                         properties: { video_url: { title: "Video link" } } } }] },
  runs: { runs: [RUN] },
  custom_steps: { steps: [] },
  schedules: { schedules: [] },
};

let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => {
  window.history.replaceState(null, "", "/?area=delivery_workflows&org=o1");
  stub.fetchLibrary.mockResolvedValue([
    { definition_id: "def_v", workflow_key: "family_video", name: "Family video",
      is_catalogue: false, version: 2, step_count: 3, run_count: 0,
      steps: [{ id: "fetch", title: "Fetch" }, { id: "edit", title: "Edit" },
              { id: "publish", title: "Publish" }] },
  ]);
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    if ((init?.method ?? "GET").toUpperCase() === "GET") {
      return Response.json(VIEWS[url.searchParams.get("view") ?? ""] ?? {});
    }
    return Response.json({ ok: true });
  }));
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  vi.unstubAllGlobals();
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
const click = async (el: HTMLElement | null) => {
  expect(el).not.toBeNull();
  await act(async () => { el!.click(); });
  await settle();
};

describe("the tabs, and the URL says which", () => {
  it("opens on Runs, counting them, and Library is one press away", async () => {
    await mount();
    expect($('[data-testid="dw-tab-runs"]')?.textContent).toBe("Runs (1)");
    expect($('[data-testid="dw-tab-runs"]')?.getAttribute("aria-selected")).toBe("true");
    await click($('[data-testid="dw-tab-library"]'));
    expect(params().get("tab")).toBe("library");
    expect(params().get("org")).toBe("o1");
    expect($('[data-testid="wf-library"]')).not.toBeNull();
    expect($('[data-testid="wf-card-family_video"]')).not.toBeNull();
  });

  it("Back returns to the tab it came from", async () => {
    await mount();
    await click($('[data-testid="dw-tab-library"]'));
    await act(async () => {
      window.history.replaceState(null, "", "/?area=delivery_workflows&org=o1&tab=runs");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await settle();
    expect($('[data-testid="wf-library"]')).toBeNull();
    expect($('[data-testid="dw-tab-runs"]')?.getAttribute("aria-selected")).toBe("true");
  });
});

describe("the editor is full width, not a tab", () => {
  it("a card's Open puts &wf= in the URL and mounts the editor on that workflow", async () => {
    window.history.replaceState(null, "", "/?area=delivery_workflows&org=o1&tab=library");
    await mount();
    await click($('[data-testid="wf-card-open-family_video"]'));
    expect(params().get("wf")).toBe("family_video");
    expect($('[data-testid="workflow-editor"]')?.dataset.key).toBe("family_video");
    // The tabs are gone while editing.
    expect($('[data-testid="dw-tab-runs"]')).toBeNull();
  });

  it("New workflow opens the editor on a new one; Back returns to the Library", async () => {
    window.history.replaceState(null, "", "/?area=delivery_workflows&tab=library");
    await mount();
    await click($('[data-testid="wf-lib-new"]'));
    expect(params().get("wf")).toBe("new");
    await click($('[data-testid="editor-back"]'));
    expect(params().get("wf")).toBeNull();
    expect(params().get("tab")).toBe("library");
    expect($('[data-testid="wf-library"]')).not.toBeNull();
  });

  it("Duplicate opens an unsaved copy and writes nothing", async () => {
    window.history.replaceState(null, "", "/?area=delivery_workflows&tab=library");
    await mount();
    const writes = () => (fetch as unknown as { mock: { calls: [unknown, RequestInit?][] } })
      .mock.calls.filter(([, init]) => (init?.method ?? "GET") !== "GET").length;
    const before = writes();
    await click($('[data-testid="wf-card-more-family_video"]'));
    const dup = [...host!.querySelectorAll<HTMLElement>('[role="menuitem"]')]
      .find((b) => b.textContent === "Duplicate") ?? null;
    await click(dup);
    expect(params().get("wf")).toBe("new");
    expect(params().get("from")).toBe("family_video");
    expect(writes()).toBe(before);
    expect(stub.renameWorkflow).not.toHaveBeenCalled();
    expect(stub.deleteWorkflow).not.toHaveBeenCalled();
  });

  it("a deep link with &wf= opens straight into the editor", async () => {
    window.history.replaceState(null, "", "/?area=delivery_workflows&wf=family_video&step=edit");
    await mount();
    expect($('[data-testid="workflow-editor"]')?.dataset.key).toBe("family_video");
  });
});

describe("every Start opens the workflow's start form, preselected (BOSSAPP-989: not a tab)", () => {
  it("from a Library card", async () => {
    window.history.replaceState(null, "", "/?area=delivery_workflows&tab=library");
    await mount();
    await click($('[data-testid="wf-card-start-family_video"]'));
    // The form draws in place of the tab panels; the URL does not change (it is not a place).
    expect(params().get("tab")).toBe("library");
    expect($('[data-testid="dw-new-project"]')).not.toBeNull();
    // Preselected: the composer is on that workflow and opens on its own start form.
    expect($('[data-testid="dw-new-project"]')?.dataset.workflow).toBe("family_video");
  });

  it("from the editor", async () => {
    window.history.replaceState(null, "", "/?area=delivery_workflows&wf=family_video");
    await mount();
    await click($('[data-testid="editor-start"]'));
    expect(params().get("wf")).toBeNull();
    // Preselected: the composer is on that workflow and opens on its own start form.
    expect($('[data-testid="dw-new-project"]')?.dataset.workflow).toBe("family_video");
  });
});

describe("a run is drawn on the lane", () => {
  it("with all eleven steps, in run mode, each told its phase", async () => {
    await mount();
    const lane = $('[data-testid="flow-lane"]');
    expect(lane?.dataset.mode).toBe("run");
    const nodes = [...lane!.querySelectorAll<HTMLElement>("[data-step-id]")];
    expect(nodes.map((n) => n.dataset.stepId)).toEqual(ELEVEN);
    expect(nodes.map((n) => n.dataset.phase)).toEqual(
      [...Array(4).fill("done"), "moving", ...Array(6).fill("future")]);
    // Named from the engine's registry, not the raw id.
    expect(nodes[0].textContent).toContain("Do research");
  });

  it("opens on the step in flight, and a node click opens that step's detail below", async () => {
    await mount();
    expect($('[data-testid="dw-detail"]')?.textContent).toContain("Do build");
    await click($('[data-step-id="review"]'));
    expect($('[data-step-id="review"]')?.dataset.selected).toBe("true");
    expect($('[data-testid="dw-detail"]')?.textContent).toContain("Do review");
  });

  it("the run's bar carries the Agents chip and opens the workflow it follows", async () => {
    await mount();
    expect($('[data-testid="dw-run-agents"]')?.getAttribute("aria-pressed")).toBe("false");
    await click($('[data-testid="dw-run-open-workflow"]'));
    expect(params().get("wf")).toBe("standard_product_delivery");
    expect($('[data-testid="workflow-editor"]')?.dataset.key).toBe("standard_product_delivery");
  });
});
