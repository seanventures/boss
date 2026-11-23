// @vitest-environment happy-dom
// @vitest-environment-options {"url": "http://console.test/?area=delivery_workflows&wf=standard_product_delivery"}
/**
 * A standard workflow is edited in place, and Reset to standard brings back what BOSS shipped.
 *
 *     "we should make all the workflows editable, and if they make mistakes we should go back
 *      to factory mode which is reset to original value. Then they can start from beginning..
 *      if they need tiny bit of change they can directly do it right?"
 *
 * The whole editor over the fake API (_helpers/fake-workflow-api.ts): an edit + Save publishes
 * the standard's next version (no copy); the chip turns Modified with "Changes from standard";
 * Reset to standard is confirmed ON THE PAGE (never window.confirm); "New standard available"
 * offers take-with-my-changes (only when clean) / take-only / not now; Save as your copy stays.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fakeWorkflowApi, type FakeApi } from "./_helpers/fake-workflow-api";
import { linear, PALETTE } from "./_helpers/canvas-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { WorkflowEditor } = await import("../app/workflows/editor/workflow-editor");

const STANDARD = { definition_id: "wfd_std", workflow_key: "standard_product_delivery",
                   name: "Standard Product Delivery", is_catalogue: true, version: 3 };
const MODIFIED = {
  modified: true, baseline_version: 3,
  changes: [{ change: "changed", step_id: "s2", text: "~ Review: agent changed" },
            { change: "added", step_id: "notify", text: "+ Notify partner, after Contract tests" }],
  new_standard: null,
};

let api: FakeApi;
let root: Root | null = null;
let host: HTMLElement | null = null;
const confirmSpy = vi.fn(() => true);

beforeEach(() => {
  window.history.replaceState(null, "", "/?area=delivery_workflows&wf=standard_product_delivery");
  api = fakeWorkflowApi(PALETTE, [{ ...STANDARD, spec: linear(4) }]);
  vi.stubGlobal("fetch", vi.fn(api.fetch));
  vi.stubGlobal("confirm", confirmSpy);
  confirmSpy.mockClear();
  try { window.localStorage.clear(); window.sessionStorage.clear(); } catch { /* none */ }
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(async () => {
  await new Promise((r) => setTimeout(r, 0));
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.querySelectorAll(".wf-pop, .wf-tip").forEach((n) => n.remove());
  vi.unstubAllGlobals();
});

async function settle(rounds = 10) {
  for (let i = 0; i < rounds; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
async function mount() {
  act(() => root!.render(<WorkflowEditor projectId="prj_1" workflowKey="standard_product_delivery"
                                         onBack={() => {}} onStart={() => {}} actor="ops@example.test" />));
  await settle();
}
const byTest = (id: string) => document.querySelector<HTMLElement>(`[data-testid="${id}"]`);
const press = async (id: string) => {
  const el = byTest(id);
  expect(el, id).not.toBeNull();
  await act(async () => { el!.click(); });
  await settle();
};
const acts = () => api.writes.filter((w) => w.action === "standard");

describe("a standard is edited in place", () => {
  it("before any edit: Standard, Save edits it, Save as your copy is secondary", async () => {
    await mount();
    expect(byTest("wf-kind")!.textContent).toBe("Standard");
    expect(byTest("wf-save")!.textContent).toBe("Save");
    expect(byTest("wf-save")!.getAttribute("aria-disabled")).toBe("true");   // nothing changed yet
    expect(byTest("wf-std-line")!.textContent).toBe("Shipped by BOSS, editable");
    expect(byTest("wf-std-save-copy")!.textContent).toBe("Save as your copy");
    expect(byTest("wf-std-reset")).toBeNull();
    expect(byTest("wf-std-changes")).toBeNull();
  });

  it("an edit + Save publishes the standard's own next version and turns it Modified", async () => {
    await mount();
    // Retitle a step through the JSON view: the editor is unlocked for a standard.
    const store = byTest("wf-view-json");
    await act(async () => { store!.click(); });
    await settle();
    const text = document.querySelector<HTMLTextAreaElement>(".wf-json textarea, textarea[data-testid='wf-json-text']");
    expect(text, "json editor").not.toBeNull();
    const spec = JSON.parse(text!.value);
    spec.steps[0].title = "Read it twice";
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(text, JSON.stringify(spec, null, 2));
      text!.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await settle();
    await press("wf-save");
    const publishes = api.writes.filter((w) => w.action === "publish");
    expect(publishes).toHaveLength(1);
    expect(publishes[0]).toMatchObject({ definition_id: "wfd_std", name: "Standard Product Delivery" });
    expect(publishes[0].as_copy).toBeUndefined();
    expect(publishes[0].update_copy_id).toBeUndefined();
    expect(api.workflows).toHaveLength(1);                       // no copy was made
    expect(byTest("wf-kind")!.textContent).toBe("Modified");
    expect(byTest("wf-version")!.textContent).toBe("v4");
    expect(byTest("wf-std-changes")!.textContent).toContain("Changes from standard · 1");
  });

  it("Modified: shows the changes, and Reset to standard is confirmed on the page", async () => {
    api.workflows[0].standard = MODIFIED;
    await mount();
    expect(byTest("wf-kind")!.textContent).toBe("Modified");
    expect(byTest("wf-std-changes")!.querySelector("summary")!.textContent).toBe("Changes from standard · 2");
    const lines = [...byTest("wf-std-change-list")!.querySelectorAll("li")];
    expect(lines.map((l) => [l.dataset.change, l.textContent])).toEqual([
      ["c", "~ Review: agent changed"], ["a", "+ Notify partner, after Contract tests"]]);
    expect(byTest("wf-std-line")!.textContent).toBe("New runs use v3");
    expect(byTest("wf-std-line")!.dataset.tip).toBe("Runs already going keep the version they started on.");

    await press("wf-std-reset");
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(byTest("wf-std-confirm")!.textContent).toContain("Your edits stay in history.");
    expect(acts()).toEqual([]);
    await press("wf-std-reset-cancel");
    expect(byTest("wf-std-confirm")).toBeNull();
    expect(acts()).toEqual([]);

    await press("wf-std-reset");
    await press("wf-std-reset-confirm");
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(acts()).toEqual([expect.objectContaining({ project_id: "prj_1", definition_id: "wfd_std", act: "reset" })]);
    // Reloaded: the standard again.
    expect(byTest("wf-kind")!.textContent).toBe("Standard");
    expect(byTest("wf-version")!.textContent).toBe("v4");
    expect(byTest("wf-std-changes")).toBeNull();
  });

  it("New standard available: take it with my changes, or only, or not now", async () => {
    api.workflows[0].standard = { ...MODIFIED, new_standard: {
      version: 5, applies_cleanly: true, conflicts: [],
      changes: [{ change: "added", step_id: "scan", text: "+ Security scan, after Fix the file" }] } };
    await mount();
    expect(byTest("wf-new-standard")!.textContent).toBe("New standard available");
    expect(byTest("wf-std-offer")).toBeNull();
    await press("wf-new-standard");
    expect(byTest("wf-std-offer-changes")!.textContent).toContain("Standard now: + Security scan, after Fix the file");
    expect(byTest("wf-std-yours")!.textContent).toContain("Review: agent changed");
    await press("wf-std-take-keep");
    expect(acts()).toEqual([expect.objectContaining({ act: "take_new_keep" })]);
    expect(byTest("wf-new-standard")).toBeNull();
  });

  it("a new standard that clashes with my changes cannot be taken with them", async () => {
    api.workflows[0].standard = { ...MODIFIED, new_standard: {
      version: 5, applies_cleanly: false, conflicts: ["step 'Review' agent"],
      changes: [{ change: "changed", step_id: "s2", text: "~ Review: agent changed" }] } };
    await mount();
    await press("wf-std-review");
    const keep = byTest("wf-std-take-keep")!;
    expect(keep.getAttribute("aria-disabled")).toBe("true");
    expect(keep.dataset.tip).toContain("step 'Review' agent");
    // Said on the page too: a phone has no hover.
    expect(byTest("wf-std-clash")!.textContent).toContain("step 'Review' agent");
    await press("wf-std-take-keep");
    expect(acts()).toEqual([]);
    await press("wf-std-not-now");
    expect(acts()).toEqual([expect.objectContaining({ act: "dismiss" })]);
    expect(byTest("wf-new-standard")).toBeNull();
  });

  it("acts wait while there are unsaved changes", async () => {
    api.workflows[0].standard = MODIFIED;
    await mount();
    // Discard needs something to discard: undo history is empty, so dirty the meta instead.
    const text = byTest("wf-view-json");
    await act(async () => { text!.click(); });
    await settle();
    const area = document.querySelector<HTMLTextAreaElement>(".wf-json textarea, textarea[data-testid='wf-json-text']")!;
    const spec = JSON.parse(area.value);
    spec.steps[0].title = "Unsaved";
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(area, JSON.stringify(spec));
      area.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await settle();
    expect(byTest("wf-std-reset")!.dataset.tip).toBe("Save or discard your changes first");
    await press("wf-std-reset");
    expect(byTest("wf-std-confirm")).toBeNull();
  });

  it("Save as your copy is still there: from the strip and from the menu", async () => {
    await mount();
    await press("wf-std-save-copy");
    expect(document.querySelector('[data-testid="wf-copy-name"]')).not.toBeNull();
    await press("wf-copy-save");
    const publishes = api.writes.filter((w) => w.action === "publish");
    expect(publishes).toEqual([expect.objectContaining({ as_copy: true, definition_id: null,
                                                         from_workflow_key: "standard_product_delivery" })]);
  });
});
