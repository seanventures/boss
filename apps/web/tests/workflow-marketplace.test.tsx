// @vitest-environment happy-dom
/**
 * The shared Library (design PR #287, BOSSAPP-1296), mounted and pressed.
 *
 *     "Library is one list, labelled BOSS or Your team, with tag filters. Add to project ...
 *      Share to Library works from a project's workflow."
 *
 * The Library tab has a "Shared library ›" button; the list labels each item BOSS or Your team,
 * with search and tag filters; a row opens its card (steps, includes, input, brief) with Add to
 * project (any project you may add to) and Edit a copy (added here, then opened in the editor).
 * An installed workflow in Yours says "from Library · vX", offers Review update when you changed
 * it, and Uninstall on its ⋯ menu; a workflow of yours shares from its ⋯ menu with tags.
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
    agents: [{ key: "dq_rca_agent", name: "DQ Investigator", present: false }], tags: ["data quality", "banking"] },
  { id: "sre-helpdesk", source: "boss", name: "SRE / Helpdesk", summary: "", publisher: "BOSS",
    version: "1.1", updated_at: null, installed: true, installed_version: "1.0", update_available: "1.1",
    waiting: [], published_here: false,
    workflows: [{ key: "incident_response", name: "Incident Response", description: "",
                  steps: ["Investigate", "Fix", "CI", "Report", "Approve"] }],
    needs: [], agents: [], tags: ["incidents", "sre"], installed_in: ["p1"] },
  { id: "wpk_1", source: "org", name: "Gateway tenant onboarding", summary: "", publisher: "Pat",
    version: "0.3", updated_at: "2026-11-22T10:00:00Z", installed: false, installed_version: null,
    update_available: null, waiting: [], published_here: true,
    workflows: [{ key: "lumen_review", name: "Lumen Pay partner review", description: "", steps: [] }],
    needs: [], agents: [], tags: ["gateway"] },
  { id: "api-gateway-onboarding", source: "boss", name: "API gateway onboarding",
    summary: "A partner's gateway config: check, fix, review, merge, staging, contract tests.", publisher: "BOSS",
    version: "1.0", updated_at: null, installed: false, installed_version: null, update_available: null,
    waiting: [], published_here: false, tags: ["gateway", "onboarding", "config"], installed_in: ["helpdesk"],
    workflows: [{ key: "api_gateway_onboarding", name: "API gateway onboarding", description: "",
                  steps: ["Fix the file", "Adversarial review", "Merge", "Staging", "Contract tests"], step_count: 5,
                  inputs: ["Start from a file", "Where the fixed file goes"],
                  objective: "Take the file the run was given, check it with the repository's declared checkers." }],
    needs: [], agents: [] },
  { id: "team_pack", source: "org", name: "Partner config review", summary: "Tell the team.", publisher: "Pat",
    version: "0.2", updated_at: null, installed: false, installed_version: null, update_available: null,
    waiting: [], published_here: false, tags: ["review"], installed_in: ["helpdesk"], used_count: 3,
    workflows: [{ key: "partner_review", name: "Partner config review", description: "", steps: ["Tell"] }],
    needs: [], agents: [] },
];

const PROJECTS = [{ id: "p1", name: "This project" }, { id: "helpdesk", name: "HelpDesk" },
                  { id: "northgate", name: "Northgate gateway" }];

let writes: Obj[] = [];
let refuseUninstall = false;
let shareBlocked = false;
const confirmSpy = vi.fn(() => true);

function fakeFetch(input: unknown, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input), "http://console.test");
  const method = (init?.method ?? "GET").toUpperCase();
  if (method === "GET") {
    const view = url.searchParams.get("view");
    if (view === "marketplace") return Promise.resolve(Response.json({ items: PACKS, projects: PROJECTS }));
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
    const key = body.pack_id === "api-gateway-onboarding" ? "api_gateway_onboarding" : "dq_incident_rca";
    return Promise.resolve(Response.json({ pack_id: body.pack_id, version: "1.0", installed: [{ key }] }));
  }
  if (body.action === "market_uninstall" && refuseUninstall) {
    return Promise.resolve(Response.json(
      { detail: "Runs are still going: Incident Response (1). Uninstall when they finish, or stop them first." },
      { status: 409 }));
  }
  if (body.action === "market_publish") {
    return Promise.resolve(Response.json({ pack: { id: "wpk_2", name: body.name, summary: body.summary,
                                                   version: body.version, status: "published" },
                                           blocked: shareBlocked ? [{ project_id: "helpdesk", offered: false,
                                             blocked: "an agent it names is missing" }] : [] }));
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
  shareBlocked = false;
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
async function choose(id: string, value: string) {
  const el = $(id) as HTMLSelectElement;
  await act(async () => { el.value = value; el.dispatchEvent(new Event("change", { bubbles: true })); });
  await settle();
}
async function type(id: string, value: string) {
  const el = $(id) as HTMLInputElement;
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  await act(async () => { setter.call(el, value); el.dispatchEvent(new Event("input", { bubbles: true })); });
  await settle();
}

describe("the shared Library (design PR #287)", () => {
  it("the Library tab has a Shared library › button that opens one labelled list", async () => {
    await mount();
    expect($("wf-library")).not.toBeNull();
    expect($("wf-lib-market")!.textContent).toContain("Shared library");
    expect($("wf-lib-market")!.querySelector("svg")).not.toBeNull();
    await press("wf-lib-market");
    expect(window.location.search).toContain("lib=market");
    expect($("wf-market")).not.toBeNull();
    expect($("wf-pack-src-banking-dq")!.textContent).toBe("BOSS");
    expect($("wf-pack-src-wpk_1")!.textContent).toBe("Your team");
    expect($("wf-pack-line-api-gateway-onboarding")!.textContent).toBe(
      "A partner's gateway config: check, fix, review, merge, staging, contract tests. · 5 steps · 1 project");
    expect($("wf-pack-installed-sre-helpdesk")!.textContent).toContain("Added");
    // The first row's card is open; a row click opens its own.
    expect($("wf-shelf-card-banking-dq")).not.toBeNull();
    await press("wf-pack-sre-helpdesk");
    expect($("wf-pack-update-sre-helpdesk")!.textContent).toBe("Update v1.1");
    expect($("wf-pack-steps-sre-helpdesk")!.textContent).toContain("Investigate");
    await press("wf-pack-wpk_1");
    expect($("wf-pack-published-wpk_1")).not.toBeNull();
    await press("wf-pack-unpublish-wpk_1");
    expect(writes).toEqual([]);                       // asked first, on the page
    expect($("wf-pack-unpublish-go-wpk_1")).not.toBeNull();
    await press("wf-market-back");
    expect($("wf-market")).toBeNull();
    expect($("wf-card-spd")).not.toBeNull();
  });

  it("search and tags narrow the one list", async () => {
    window.history.replaceState(null, "", "/?area=delivery_workflows&tab=library&lib=market");
    await mount();
    await press("wf-shelf-tag-gateway");
    expect($("wf-pack-api-gateway-onboarding")).not.toBeNull();
    expect($("wf-pack-banking-dq")).toBeNull();
    await press("wf-shelf-tag-all");
    await type("wf-shelf-search", "incident");
    expect($("wf-pack-sre-helpdesk")).not.toBeNull();
    expect($("wf-pack-api-gateway-onboarding")).toBeNull();
    await type("wf-shelf-search", "nothing like this");
    expect($("wf-market-empty")!.textContent).toBe("Nothing matches.");
  });

  it("Add to project: pick a project, press Add, and it is added there", async () => {
    window.history.replaceState(null, "", "/?area=delivery_workflows&tab=library&lib=market");
    await mount();
    await press("wf-pack-api-gateway-onboarding");
    const card = $("wf-shelf-card-api-gateway-onboarding")!;
    expect(card.textContent).toContain("Start from a file");          // Input
    expect(card.textContent).toContain("Full brief");
    const add = $("wf-pack-install-api-gateway-onboarding")!;
    expect(add.className).toContain("wf-btn-primary");
    expect(add.querySelector("svg")).not.toBeNull();
    // Added to HelpDesk already: the button says so for that project.
    await choose("wf-pack-project-api-gateway-onboarding", "helpdesk");
    expect($("wf-pack-install-api-gateway-onboarding")!.textContent).toContain("Added");
    expect(($("wf-pack-install-api-gateway-onboarding") as HTMLButtonElement).disabled).toBe(true);
    await choose("wf-pack-project-api-gateway-onboarding", "northgate");
    await press("wf-pack-install-api-gateway-onboarding");
    expect(writes).toEqual([{ action: "market_install", project_id: "northgate", pack_id: "api-gateway-onboarding" }]);
    expect($("wf-pack-said-api-gateway-onboarding")!.textContent).toContain("Added to Northgate gateway.");
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("says how a copy takes new versions: BOSS's by itself, a team's waits for review", async () => {
    window.history.replaceState(null, "", "/?area=delivery_workflows&tab=library&lib=market");
    await mount();
    await press("wf-pack-api-gateway-onboarding");
    expect($("wf-pack-hint-api-gateway-onboarding")!.textContent).toBe("Follows Library updates until you change it.");
    await press("wf-pack-team_pack");
    expect($("wf-pack-hint-team_pack")!.textContent).toBe("New versions wait for your review.");
    // Used counts the whole org; the ids name only projects this person may add to.
    expect($("wf-pack-line-team_pack")!.textContent).toContain("3 projects");
  });

  it("a share that some projects could not take names them before closing", async () => {
    shareBlocked = true;
    await mount();
    await press("wf-card-more-lumen_review");
    await press("wf-card-publish-lumen_review");
    await press("wf-publish-submit");
    expect($("wf-publish-blocked")!.textContent).toContain("1 project could not take it");
    expect($("wf-publish-blocked")!.textContent).toContain("helpdesk: an agent it names is missing");
    await press("wf-publish-done");
    expect($("wf-publish-blocked")).toBeNull();
  });

  it("Edit a copy adds it to this project and opens the copy in the editor", async () => {
    window.history.replaceState(null, "", "/?area=delivery_workflows&tab=library&lib=market");
    const p = props();
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root!.render(<Library {...p} />));
    await settle();
    await press("wf-pack-api-gateway-onboarding");
    const edit = $("wf-pack-edit-api-gateway-onboarding")!;
    expect(edit.textContent).toContain("Edit a copy");
    expect(edit.querySelector("svg")).not.toBeNull();
    await press("wf-pack-edit-api-gateway-onboarding");
    expect(writes).toEqual([{ action: "market_install", project_id: "p1", pack_id: "api-gateway-onboarding" }]);
    expect(p.onOpen).toHaveBeenCalledWith("api_gateway_onboarding");
    // Already here: Edit a copy only opens it.
    writes = [];
    await press("wf-pack-sre-helpdesk");
    await press("wf-pack-edit-sre-helpdesk");
    expect(writes).toEqual([]);
    expect(p.onOpen).toHaveBeenLastCalledWith("incident_response");
  });

  it("an installed workflow is in Yours, from Library, with Review update when you changed it", async () => {
    await mount();
    expect($("wf-card-from-market-incident_response")!.textContent).toBe("from Library · v1.0 · Modified");
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

  it("a workflow of yours shares to your org Library; the community registry is for later", async () => {
    await mount();
    await press("wf-card-more-lumen_review");
    await press("wf-card-publish-lumen_review");
    expect(($("wf-publish-name") as HTMLInputElement).value).toBe("Lumen Pay partner review");
    expect(($("wf-publish-version") as HTMLInputElement).value).toBe("1.0");
    expect(($("wf-publish-org") as HTMLInputElement).checked).toBe(true);
    expect(($("wf-publish-community") as HTMLInputElement).disabled).toBe(true);
    expect($("wf-publish-submit")!.querySelector("svg")).not.toBeNull();
    await type("wf-publish-name", "Gateway tenant onboarding");
    await type("wf-publish-version", "0.3");
    await type("wf-publish-tags", "gateway, onboarding");
    await press("wf-publish-submit");
    expect(writes).toEqual([{ action: "market_publish", project_id: "p1", definition_id: "def_lumen_review",
                              name: "Gateway tenant onboarding", summary: "Check a partner's gateway config.",
                              version: "0.3", tags: ["gateway", "onboarding"] }]);
  });

  it("a standard has no Share or Uninstall", async () => {
    await mount();
    await press("wf-card-more-spd");
    const menu = $("wf-card-menu-spd")!.textContent ?? "";
    expect(menu).not.toContain("Share");
    expect(menu).not.toContain("Uninstall");
  });
});
