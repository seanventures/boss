// @vitest-environment happy-dom
/**
 * The Workflow Marketplace, mounted and pressed (approved wireframe, 2026-11-22).
 *
 *     "we dont have to ship by default we should have Install button once installed it will be
 *      showing in their custom / local workflows and not in Standard ones. When people build the
 *      workflow, they can publish to the Marketplace and they can be installed into the app
 *      using Install button."
 *
 * The Library stays one list with a "Marketplace ›" button; pack cards say publisher, version
 * and contents, and Install / Installed ✓ / Update; Install is confirmed ON THE PAGE (never
 * window.confirm) with what it adds, needs and brings; an installed workflow in Yours says
 * "from Marketplace · vX", offers Review update when you changed it, and Uninstall on its ⋯ menu
 * (a refusal is said on the page); a workflow of yours publishes from its ⋯ menu, the community
 * marketplace greyed for later.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkflowSummary } from "../lib/workflow-editor-types";

vi.mock("../app/workflows/studio-parts", () => import("./_helpers/studio-parts-stub"));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { Library } = await import("../app/workflows/library");
const stub = await import("./_helpers/studio-parts-stub");

type Obj = Record<string, unknown>;

const wf = (over: Partial<WorkflowSummary>): WorkflowSummary => ({
  definition_id: `def_${over.workflow_key}`, workflow_key: "k", name: "K", is_catalogue: false,
  version: 1, step_count: 2, run_count: 0,
  steps: [{ id: "a", title: "Investigate" }, { id: "b", title: "Build" }],
  ...over,
});

const INSTALLED = wf({
  workflow_key: "incident_response", name: "Incident Response", version: 3,
  marketplace: { pack_id: "sre-helpdesk", pack_name: "SRE / Helpdesk", source: "boss", version: "1.0",
                 modified: true, waiting: null, update: { version: "1.1" } },
});
const MINE = wf({ workflow_key: "lumen_review", name: "Lumen Pay partner review",
                  description: "Check a partner's gateway config." });
const LIBRARY = [
  wf({ workflow_key: "spd", name: "Standard Product Delivery", is_catalogue: true, version: 4 }),
  INSTALLED, MINE,
];

const PACKS = [
  { id: "banking-dq", source: "boss", name: "Banking Data Quality", summary: "", publisher: "BOSS",
    version: "1.0", updated_at: null, installed: false, installed_version: null, update_available: null,
    waiting: [], published_here: false,
    workflows: [{ key: "dq_incident_rca", name: "DQ RCA and Remediation", description: "", steps: [] },
                { key: "dq_incident_escalation", name: "DQ Incident Escalation", description: "", steps: [] },
                { key: "dq_monitor_intake", name: "DQ Monitor Intake", description: "", steps: [] }],
    needs: [{ key: "ontology:banking-dq", label: "Knowledge kinds", ok: true, note: "added" },
            { key: "mcp:dq-monitor", label: "Connector dq-monitor", ok: false, note: "not set up" }],
    agents: [{ key: "dq_rca_agent", name: "DQ Investigator", present: false }] },
  { id: "sre-helpdesk", source: "boss", name: "SRE / Helpdesk", summary: "", publisher: "BOSS",
    version: "1.1", updated_at: null, installed: true, installed_version: "1.0", update_available: "1.1",
    waiting: [], published_here: false,
    workflows: [{ key: "incident_response", name: "Incident Response", description: "",
                  steps: ["Investigate", "Fix", "CI", "Report", "Approve"] }],
    needs: [], agents: [] },
  { id: "wpk_1", source: "org", name: "Gateway tenant onboarding", summary: "", publisher: "Pat",
    version: "0.3", updated_at: "2026-11-22T10:00:00Z", installed: false, installed_version: null,
    update_available: null, waiting: [], published_here: true,
    workflows: [{ key: "lumen_review", name: "Lumen Pay partner review", description: "", steps: [] }],
    needs: [], agents: [] },
];

let writes: Obj[] = [];
let refuseUninstall = false;
const confirmSpy = vi.fn(() => true);

function fakeFetch(input: unknown, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input), "http://console.test");
  const method = (init?.method ?? "GET").toUpperCase();
  if (method === "GET") {
    const view = url.searchParams.get("view");
    if (view === "marketplace") return Promise.resolve(Response.json({ items: PACKS }));
    if (view === "spec") {
      return Promise.resolve(Response.json({
        definition_id: INSTALLED.definition_id, workflow_key: INSTALLED.workflow_key, name: INSTALLED.name,
        is_catalogue: false, version: 3, spec: { steps: [] }, versions: [], standard: null,
        marketplace: { ...INSTALLED.marketplace, update: { version: "1.1", applies_cleanly: true, conflicts: [],
          changes: [{ change: "changed", step_id: "investigate", text: "~ Investigate: name changed" }] } },
      }));
    }
    return Promise.resolve(Response.json({}));
  }
  const body = init?.body ? JSON.parse(String(init.body)) as Obj : {};
  writes.push(body);
  if (body.action === "market_install") {
    return Promise.resolve(Response.json({ pack_id: body.pack_id, version: "1.0",
                                           installed: [{ key: "dq_incident_rca" }] }));
  }
  if (body.action === "market_uninstall" && refuseUninstall) {
    return Promise.resolve(Response.json(
      { detail: "Runs are still going: Incident Response (1). Uninstall when they finish, or stop them first." },
      { status: 409 }));
  }
  if (body.action === "market_publish") {
    return Promise.resolve(Response.json({ pack: { id: "wpk_2", name: body.name, summary: body.summary,
                                                   version: body.version, status: "published" } }));
  }
  if (body.action === "market_update") return Promise.resolve(Response.json({ marketplace: null }));
  return Promise.resolve(Response.json({ ok: true }));
}

const props = () => ({
  projectId: "p1", busy: null, schedules: [], inputSchemas: {}, examples: {},
  workClasses: ["project.new"], riskFloor: 2, customSteps: [],
  onStart: vi.fn(), onOpen: vi.fn(), onNew: vi.fn(), onDuplicate: vi.fn(), onUseFor: vi.fn(),
  onSaveSchedule: vi.fn(async () => true), onToggleSchedule: vi.fn(), onDeleteSchedule: vi.fn(),
  onRemoveStep: vi.fn(), onStepsChanged: vi.fn(),
});

let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => {
  window.history.replaceState(null, "", "/?area=delivery_workflows&tab=library");
  stub.fetchLibrary.mockReset().mockResolvedValue(LIBRARY);
  writes = [];
  refuseUninstall = false;
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
  vi.stubGlobal("confirm", confirmSpy);
  confirmSpy.mockClear();
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 12; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
async function mount() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(<Library {...props()} />));
  await settle();
}
const $ = (id: string) => document.querySelector<HTMLElement>(`[data-testid="${id}"]`);
async function press(id: string) {
  const el = $(id);
  expect(el, id).not.toBeNull();
  await act(async () => { el!.click(); });
  await settle();
}
async function type(id: string, value: string) {
  const el = $(id) as HTMLInputElement;
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  await act(async () => { setter.call(el, value); el.dispatchEvent(new Event("input", { bubbles: true })); });
  await settle();
}

describe("the Workflow Marketplace", () => {
  it("the Library stays one list with a Marketplace › button that opens the packs", async () => {
    await mount();
    expect($("wf-library")).not.toBeNull();
    expect($("wf-lib-market")!.textContent).toContain("Marketplace");
    await press("wf-lib-market");
    expect(window.location.search).toContain("lib=market");
    expect($("wf-market")).not.toBeNull();
    expect($("wf-pack-meta-banking-dq")!.textContent).toBe("BOSS · v1.0 · 3 workflows");
    expect($("wf-pack-meta-wpk_1")!.textContent).toBe("Your org · v0.3 · 1 workflow");
    expect($("wf-pack-sre-helpdesk")!.textContent).toContain("Incident Response: Investigate → Fix → CI");
    expect($("wf-pack-installed-sre-helpdesk")!.textContent).toContain("Installed");
    expect($("wf-pack-update-sre-helpdesk")!.textContent).toBe("Update v1.1");
    expect($("wf-pack-published-wpk_1")).not.toBeNull();
    await press("wf-pack-unpublish-wpk_1");
    expect(writes).toEqual([]);                       // asked first, on the page
    expect($("wf-pack-unpublish-go-wpk_1")).not.toBeNull();
    await press("wf-market-filter-org");
    expect($("wf-pack-banking-dq")).toBeNull();
    expect($("wf-pack-wpk_1")).not.toBeNull();
    await press("wf-market-back");
    expect($("wf-market")).toBeNull();
    expect($("wf-card-spd")).not.toBeNull();
  });

  it("Install is confirmed on the page with what it adds, needs and brings", async () => {
    window.history.replaceState(null, "", "/?area=delivery_workflows&tab=library&lib=market");
    await mount();
    await press("wf-pack-install-banking-dq");
    const confirm = $("wf-market-confirm")!;
    expect(confirm.textContent).toContain("Install Banking Data Quality v1.0?");
    expect($("wf-market-adds")!.textContent).toBe(
      "DQ RCA and Remediation · DQ Incident Escalation · DQ Monitor Intake");
    expect($("wf-market-needs")!.textContent).toContain("not set up");
    expect($("wf-market-needs")!.textContent).toContain("runs wait until it is");
    expect($("wf-market-agents")!.textContent).toContain("DQ Investigator (added if missing)");
    expect(writes).toEqual([]);
    await press("wf-market-install");
    expect(writes).toEqual([{ action: "market_install", project_id: "p1", pack_id: "banking-dq" }]);
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("an installed workflow is in Yours, from Marketplace, with Review update when you changed it", async () => {
    await mount();
    expect($("wf-card-from-market-incident_response")!.textContent).toBe("from Marketplace · v1.0 · Modified");
    expect($("wf-card-incident_response")!.textContent).toContain("Yours");
    expect($("wf-card-incident_response")!.textContent).not.toContain("Standard");
    await press("wf-card-update-incident_response");
    expect($("wf-update-changes")!.textContent).toContain("Investigate: name changed");
    await press("wf-update-take-keep");
    expect(writes).toEqual([{ action: "market_update", project_id: "p1",
                              definition_id: "def_incident_response", act: "take_keep" }]);
  });

  it("Uninstall is on the ⋯ menu, confirmed on the page, and a refusal is said there", async () => {
    refuseUninstall = true;
    await mount();
    await press("wf-card-more-incident_response");
    await press("wf-card-uninstall-incident_response");
    expect($("wf-card-uninstall-confirm-incident_response")!.textContent).toContain("Uninstall SRE / Helpdesk?");
    await press("wf-card-uninstall-go-incident_response");
    expect(writes).toEqual([{ action: "market_uninstall", project_id: "p1", pack_id: "sre-helpdesk" }]);
    expect($("wf-card-uninstall-said-incident_response")!.textContent).toContain("Runs are still going");
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("a workflow of yours publishes to your org; the community marketplace is for later", async () => {
    await mount();
    await press("wf-card-more-lumen_review");
    await press("wf-card-publish-lumen_review");
    expect(($("wf-publish-name") as HTMLInputElement).value).toBe("Lumen Pay partner review");
    expect(($("wf-publish-version") as HTMLInputElement).value).toBe("1.0");
    expect(($("wf-publish-org") as HTMLInputElement).checked).toBe(true);
    expect(($("wf-publish-community") as HTMLInputElement).disabled).toBe(true);
    await type("wf-publish-name", "Gateway tenant onboarding");
    await type("wf-publish-version", "0.3");
    await press("wf-publish-submit");
    expect(writes).toEqual([{ action: "market_publish", project_id: "p1", definition_id: "def_lumen_review",
                              name: "Gateway tenant onboarding", summary: "Check a partner's gateway config.",
                              version: "0.3" }]);
  });

  it("a standard has no Publish or Uninstall", async () => {
    await mount();
    await press("wf-card-more-spd");
    const menu = $("wf-card-menu-spd")!.textContent ?? "";
    expect(menu).not.toContain("Publish");
    expect(menu).not.toContain("Uninstall");
  });
});
