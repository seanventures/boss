// @vitest-environment happy-dom
// @vitest-environment-options {"url": "http://console.test/?area=delivery_workflows&wf=incident_response"}
/**
 * The editor on a Marketplace install and on a workflow of yours (approved wireframe, 2026-11-22).
 *
 * An installed workflow is Yours, with a strip that says where it came from and offers Review
 * update when you changed it; its ⋯ menu has Uninstall (confirmed on the page) instead of Delete.
 * A workflow of yours has Publish to Marketplace… on its ⋯ menu. A standard has neither.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fakeWorkflowApi, type FakeApi } from "./_helpers/fake-workflow-api";
import { linear, PALETTE } from "./_helpers/canvas-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { WorkflowEditor } = await import("../app/workflows/editor/workflow-editor");

const INSTALLED = { definition_id: "wfd_ir", workflow_key: "incident_response", name: "Incident Response",
                    is_catalogue: false, version: 3,
                    marketplace: { pack_id: "sre-helpdesk", pack_name: "SRE / Helpdesk", source: "boss",
                                   version: "1.0", modified: true, waiting: null,
                                   update: { version: "1.1", applies_cleanly: true, conflicts: [],
                                             changes: [{ change: "changed", step_id: "s1", text: "~ Investigate: name changed" }] } } };
const MINE = { definition_id: "wfd_mine", workflow_key: "lumen_review", name: "Lumen Pay partner review",
               is_catalogue: false, version: 2,
               published: { pack_id: "wpk_1", name: "Gateway tenant onboarding", summary: "", version: "0.3",
                            status: "published" } };
const STANDARD = { definition_id: "wfd_std", workflow_key: "standard_product_delivery",
                   name: "Standard Product Delivery", is_catalogue: true, version: 3 };

let api: FakeApi;
let root: Root | null = null;
let host: HTMLElement | null = null;
const confirmSpy = vi.fn(() => true);

beforeEach(() => {
  api = fakeWorkflowApi(PALETTE, [{ ...INSTALLED, spec: linear(3) }, { ...MINE, spec: linear(3) },
                                  { ...STANDARD, spec: linear(3) }]);
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
async function mount(key: string) {
  window.history.replaceState(null, "", `/?area=delivery_workflows&wf=${key}`);
  act(() => root!.render(<WorkflowEditor projectId="prj_1" workflowKey={key}
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

describe("the editor and the Marketplace", () => {
  it("an install says where it came from and offers the update when you changed it", async () => {
    await mount("incident_response");
    expect(byTest("wf-kind")!.textContent).toBe("Yours");
    expect(byTest("wf-standard")).toBeNull();
    expect(byTest("wf-market-line")!.textContent).toBe("From Library · SRE / Helpdesk v1.0 · Modified");
    await press("wf-market-review");
    expect(byTest("wf-update-changes")!.textContent).toContain("Investigate: name changed");
    await press("wf-update-take-keep");
    expect(api.writes.filter((w) => w.action === "market_update")).toEqual([
      expect.objectContaining({ project_id: "prj_1", definition_id: "wfd_ir", act: "take_keep" })]);
  });

  it("an install's ⋯ menu has Uninstall, confirmed on the page, instead of Delete", async () => {
    await mount("incident_response");
    await press("wf-more");
    expect(byTest("wf-menu-delete")).toBeNull();
    expect(byTest("wf-menu-publish")).toBeNull();
    await press("wf-menu-uninstall");
    expect(byTest("wf-uninstall")!.textContent).toContain("Uninstall SRE / Helpdesk?");
    await press("wf-uninstall-confirm");
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(api.writes.filter((w) => w.action === "market_uninstall")).toEqual([
      expect.objectContaining({ project_id: "prj_1", pack_id: "sre-helpdesk" })]);
  });

  it("a workflow of yours publishes again as the next version from its ⋯ menu", async () => {
    await mount("lumen_review");
    expect(byTest("wf-market-strip")).toBeNull();
    await press("wf-more");
    expect(byTest("wf-menu-publish")!.textContent).toBe("Share again…");
    await press("wf-menu-publish");
    expect((byTest("wf-publish-name") as HTMLInputElement).value).toBe("Gateway tenant onboarding");
    expect((byTest("wf-publish-version") as HTMLInputElement).value).toBe("0.4");
    await press("wf-publish-submit");
    expect(api.writes.filter((w) => w.action === "market_publish")).toEqual([
      expect.objectContaining({ project_id: "prj_1", definition_id: "wfd_mine", version: "0.4",
                                name: "Gateway tenant onboarding" })]);
  });

  it("a standard has neither Publish nor Uninstall", async () => {
    await mount("standard_product_delivery");
    await press("wf-more");
    expect(byTest("wf-menu-publish")).toBeNull();
    expect(byTest("wf-menu-uninstall")).toBeNull();
    expect(byTest("wf-menu-delete")).not.toBeNull();
  });
});
