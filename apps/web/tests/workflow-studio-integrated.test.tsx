// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fakeWorkflowApi, type FakeApi } from "./_helpers/fake-workflow-api";
import { linear, PALETTE } from "./_helpers/canvas-fixtures";

/**
 * The Workflows area with every Workflow Studio part REAL — the editor, the Library, the run
 * lane, the store and the API client. Only `fetch` is a stand-in, answering in the API's wire
 * shapes (_helpers/fake-workflow-api.ts).
 *
 * The other area tests stand the studio parts in (studio-parts-stub) and each part has its own
 * tests; this is the one place they are mounted together, so a part that is still a stub, a
 * prop the area forgets to pass, or a hand-off between Library, editor and Runs that goes
 * nowhere fails here.
 */
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { DeliveryWorkflows } = await import("../app/delivery-workflows-ui");

const OWN = { definition_id: "wfd_own", workflow_key: "my_flow", name: "My flow", is_catalogue: false, version: 3 };

const runStep = (id: string, capability: string, status: string) => ({
  step_id: id, kind: "capability", status, capability, output: {}, evidence: [], error: null,
  approval: {}, due_at: null, timeout_applied: null, attempt: 1, started_at: null,
  completed_at: null, decision_id: null,
});

let api: FakeApi;
let runs: unknown[] = [];
let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => {
  api = fakeWorkflowApi(PALETTE, [{ ...OWN, spec: linear(4) }]);
  runs = [];
  const area: Record<string, () => unknown> = {
    runs: () => ({ runs }),
    catalogue: () => ({ items: [] }),
    installed: () => ({ items: [{ id: OWN.definition_id, workflow_key: OWN.workflow_key, name: OWN.name,
                                  is_catalogue: false, forked_from_version_id: null, namespace: "product",
                                  used_for: ["project.new"] }] }),
    schedules: () => ({ schedules: [] }),
  };
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    const view = url.searchParams.get("view") ?? "";
    if ((init?.method ?? "GET") === "GET" && url.pathname === "/api/delivery-workflows" && area[view]) {
      return Response.json(area[view]());
    }
    return api.fetch(input, init);
  }));
  try { window.localStorage.clear(); } catch { /* none */ }
});

afterEach(async () => {
  await new Promise((r) => setTimeout(r, 0));
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.querySelectorAll(".wf-pop, .wf-tip").forEach((n) => n.remove());
  vi.unstubAllGlobals();
});

async function settle(rounds = 12) {
  for (let i = 0; i < rounds; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

async function mount(search: string) {
  window.history.replaceState(null, "", `/?area=delivery_workflows${search}`);
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<DeliveryWorkflows projectId="prj_1" actor="ceo" />));
  await settle();
  return host;
}

const byTest = (id: string) => document.querySelector<HTMLElement>(`[data-testid="${id}"]`);
const click = async (el: Element | null) => {
  expect(el).not.toBeNull();
  await act(async () => { (el as HTMLElement).click(); });
  await settle();
};
const writes = () => api.writes.filter((w) => w.action !== "check");

describe("the Workflows area, with the real studio", () => {
  it("&wf= opens the real editor on that workflow, and ← Library lands on the real Library", async () => {
    await mount("&wf=my_flow");
    expect(byTest("wf-topbar")).not.toBeNull();
    expect((byTest("wf-name") as HTMLInputElement | null)?.value ?? byTest("wf-name")?.textContent).toContain("My flow");
    expect(byTest("wf-kind")?.textContent).toBe("Yours");
    // Its steps are drawn on the lane, named from the palette rather than by id.
    const titles = [...document.querySelectorAll(".wf-lane .wf-node")].map((n) => n.textContent ?? "");
    expect(titles.some((t) => t.includes("Research"))).toBe(true);

    await click(byTest("wf-back"));
    expect(byTest("wf-topbar")).toBeNull();
    expect(byTest("wf-library")).not.toBeNull();
    expect(byTest("wf-card-my_flow")).not.toBeNull();
    expect(new URLSearchParams(window.location.search).get("tab")).toBe("library");
  });

  it("the Library's Duplicate opens an unsaved copy in the editor and writes nothing", async () => {
    await mount("&tab=library");
    await click(byTest("wf-card-more-my_flow"));
    await click([...document.querySelectorAll('[role="menuitem"]')].find((b) => b.textContent?.trim() === "Duplicate") ?? null);
    await settle();
    expect(byTest("wf-topbar")).not.toBeNull();
    const name = byTest("wf-name") as HTMLInputElement;
    expect(name.value ?? name.textContent).toBe("My flow (copy)");
    expect(document.querySelectorAll(".wf-lane .wf-node")).toHaveLength(4);
    expect(writes()).toEqual([]);
    expect(new URLSearchParams(window.location.search).get("from")).toBeNull();
  });

  it("an open run is drawn on the real lane from the version it ran, and Open workflow opens it", async () => {
    runs = [{
      id: "wfr_1", status: "running", workflow_key: "my_flow", workflow_name: "My flow", version: 3,
      step_count: 4, steps_done: 1, current_step: "meeting", waiting_on: null, blocked_reason: null,
      error: null, updated_at: null, input: { title: "Shift swap board" }, finished: false,
      standing: "active", actions: [],
      steps: [runStep("research", "research", "succeeded"), runStep("meeting", "meeting", "running"),
              runStep("spec_review", "spec_review", "pending"), runStep("build", "build", "pending")],
    }];
    await mount("&wfr=wfr_1");
    const lane = byTest("dw-run-lane");
    expect(lane).not.toBeNull();
    const nodes = [...lane!.querySelectorAll<HTMLElement>(".wf-node")];
    expect(nodes.map((n) => n.dataset.stepId)).toEqual(["research", "meeting", "spec_review", "build"]);
    expect(nodes[1].textContent).toContain("Convene a panel");
    expect(nodes[1].getAttribute("data-phase")).toBe("moving");

    await click(byTest("dw-run-open-workflow"));
    expect(byTest("wf-topbar")).not.toBeNull();
    expect(new URLSearchParams(window.location.search).get("wf")).toBe("my_flow");
  });
});
