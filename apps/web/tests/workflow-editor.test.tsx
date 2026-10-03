// @vitest-environment happy-dom
// @vitest-environment-options {"url": "http://console.test/?area=delivery_workflows&wf=standard_product_delivery"}

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { overBudget, paragraphs } from "./_helpers/copy-budget";
import { fakeWorkflowApi, type FakeApi } from "./_helpers/fake-workflow-api";
import { linear, PALETTE } from "./_helpers/canvas-fixtures";

/**
 * The workflow editor, mounted whole: the shell (top bar, summary, palette, lane, QuickAdd, the
 * new-workflow screen, keys, URL) over the real store, API client, save flow, inspector and JSON
 * view. Only `fetch` is a stand-in (_helpers/fake-workflow-api.ts), so every assertion about a
 * write is about the request the console would really send.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { WorkflowEditor } = await import("../app/workflows/editor/workflow-editor");
const { NO_MODEL_TIP } = await import("../app/workflows/editor/empty-state");

const STANDARD = { definition_id: "wfd_std", workflow_key: "standard_product_delivery",
                   name: "Standard Product Delivery", is_catalogue: true, version: 3 };
const OWN = { definition_id: "wfd_own", workflow_key: "my_flow", name: "My flow", is_catalogue: false, version: 3 };

let api: FakeApi;
let root: Root | null = null;
let host: HTMLElement | null = null;
let bodyWidth = 0;
let laneWidth = 4000;
const realClientWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");
const onBack = vi.fn();
const onStart = vi.fn();

beforeEach(() => {
  bodyWidth = 0;
  laneWidth = 4000;
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get() {
      const el = this as HTMLElement;
      if (el.classList?.contains("wf-body")) return bodyWidth;
      return el.classList?.contains("wf-lane-viewport") ? laneWidth : 0;
    },
  });
  window.history.replaceState(null, "", "/?area=delivery_workflows&wf=standard_product_delivery");
  api = fakeWorkflowApi(PALETTE, [
    { ...STANDARD, spec: linear(4) },
    { ...OWN, spec: linear(4) },
  ]);
  vi.stubGlobal("fetch", vi.fn(api.fetch));
  onBack.mockReset();
  onStart.mockReset();
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
  if (realClientWidth) Object.defineProperty(HTMLElement.prototype, "clientWidth", realClientWidth);
  else delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth;
});

async function settle(rounds = 8) {
  for (let i = 0; i < rounds; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

async function mount(key = "standard_product_delivery") {
  act(() => root!.render(<WorkflowEditor projectId="prj_1" workflowKey={key} onBack={onBack} onStart={onStart}
                                         actor="ops@example.test" />));
  await settle();
  return host!;
}

const q = <E extends Element = HTMLElement>(sel: string) => document.querySelector<E>(sel) as E;
const byTest = (id: string) => q(`[data-testid="${id}"]`);
const node = (id: string) => q(`.wf-lane .wf-node[data-step-id="${id}"]`);
const laneIds = () => [...document.querySelectorAll<HTMLElement>(".wf-editor .wf-stage .wf-lane .wf-node")]
  .sort((a, b) => parseFloat(a.parentElement!.style.left) - parseFloat(b.parentElement!.style.left))
  .map((n) => n.dataset.stepId);
const click = (el: Element) => act(() => (el as HTMLElement).click());
function key(target: EventTarget, k: string, init: KeyboardEventInit = {}) {
  act(() => { target.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true, ...init })); });
}
function type(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
  act(() => {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
const publishes = () => api.writes.filter((w) => w.action === "publish");
const nonChecks = () => api.writes.filter((w) => w.action !== "check");

describe("the bar stays one row", () => {
  const css = () => readFileSync("app/workflows/editor.css", "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

  it("holds every control in one row that does not wrap at a 1100px bar or wider", async () => {
    await mount();
    const row = byTest("wf-topbar-row");
    for (const id of ["wf-back", "wf-name", "wf-version", "wf-kind", "wf-undo", "wf-view-visual", "wf-describe",
                      "wf-check", "wf-start", "wf-save", "wf-more"]) {
      expect(row.contains(byTest(id)), id).toBe(true);
    }
    const sheet = css();
    expect(sheet).toMatch(/\.wf-topbar-row \{[^}]*flex-wrap: nowrap;/);
    // Nothing on the bar's row wraps outside the narrow-bar container query.
    const outside = sheet.replace(/@container[^{]*\{[^}]*\{[^}]*\}\s*\}/g, "");
    expect(outside).not.toMatch(/\.wf-topbar-row[^{]*\{[^}]*flex-wrap: wrap/);
    // The query is strict: a bar of exactly 1100px (its padding 2 × 12px and border 2 × 1px off,
    // 1074px of content) never matches it. `max-width: 1074px` would.
    const q = sheet.match(/@container wf-topbar \(([^)]*)\)/)![1];
    expect(q).toMatch(/^width < \d+px$/);
    expect(Number(q.match(/\d+/)![0])).toBeLessThanOrEqual(1100 - 26);
    expect(sheet).toMatch(/\.wf-topbar \{[^}]*container: wf-topbar \/ inline-size;/);
  });

  it("wraps by group, never item by item: the actions move as one block and ⋯ stays with Save", async () => {
    await mount();
    const row = byTest("wf-topbar-row");
    const groups = [...row.children] as HTMLElement[];
    expect(groups.map((g) => g.dataset.testid)).toEqual(["wf-bar-identity", "wf-bar-actions"]);
    const identity = byTest("wf-bar-identity");
    for (const id of ["wf-back", "wf-name", "wf-version", "wf-kind"]) expect(identity.contains(byTest(id)), id).toBe(true);
    const actions = byTest("wf-bar-actions");
    for (const id of ["wf-undo", "wf-redo", "wf-view-visual", "wf-describe", "wf-check", "wf-start", "wf-save", "wf-more"]) {
      expect(actions.contains(byTest(id)), id).toBe(true);
    }
    // ⋯ shares its unbreakable group with Save, Start and Check, so no width leaves it alone.
    const commit = byTest("wf-bar-commit");
    expect(commit.contains(byTest("wf-more")) && commit.contains(byTest("wf-save"))).toBe(true);
    const sheet = css();
    expect(sheet).toMatch(/\.wf-bar-tools, \.wf-bar-commit \{[^}]*flex: none; flex-wrap: nowrap;/);
    expect(sheet).toMatch(/\.wf-bar-actions \{[^}]*justify-content: flex-end;[^}]*margin-left: auto;/);
  });

  it("gives way at the name first: it is cut with an ellipsis and the whole name is its tooltip", async () => {
    await mount();
    const name = byTest("wf-name");
    expect(name.dataset.tip).toBe("Standard Product Delivery");
    expect(name.dataset.tipWhen).toBe("cut");
    const sheet = css();
    expect(sheet).toMatch(/\.wf-bar-identity > \* \{ flex: none; white-space: nowrap; \}/);
    expect(sheet).toMatch(/\.wf-bar-identity > \.wf-name \{ flex: 0 1 [^;]+; min-width: [^;]+; \}/);
    expect(sheet).toMatch(/\.wf-name-input \{[^}]*min-width: 0;[^}]*text-overflow: ellipsis;/);
  });
});

describe("the bar and the summary", () => {
  it("says what is open: name, version, Standard, and Save as my copy", async () => {
    await mount();
    expect((byTest("wf-name") as HTMLInputElement).value).toBe("Standard Product Delivery");
    expect(byTest("wf-version").textContent).toBe("v3");
    expect(byTest("wf-version").dataset.tip).toBe("Each save is a new version. Running work keeps its version.");
    expect(byTest("wf-kind").textContent).toBe("Standard");
    expect(byTest("wf-kind").dataset.tip).toBe("Shipped with BOSS. Saving makes your own copy.");
    expect(byTest("wf-save").textContent).toBe("Save as my copy");
    expect(byTest("wf-check").dataset.tip).toBe("Runs every check Save runs, without saving.");
  });

  it("opens the copy you already have", async () => {
    api.workflows[0].copy = { definition_id: "wfd_own", workflow_key: "my_flow", name: "My flow" };
    await mount();
    expect(byTest("wf-open-copy").textContent).toBe("You have a copy · Open");
    click(byTest("wf-open-copy"));
    await settle();
    expect(new URLSearchParams(window.location.search).get("wf")).toBe("my_flow");
    expect((byTest("wf-name") as HTMLInputElement).value).toBe("My flow");
  });

  it("counts from the draft: steps, agents, approvals, start-form inputs", async () => {
    api.workflows[0].spec = { ...linear(10, (i) => (i === 1 ? { captain: "qa" } : {})),
                              input_schema: { required: ["repo"], properties: { repo: {}, brief: {} } } };
    await mount();
    expect(byTest("wf-chip-steps").textContent).toBe("10 steps");
    expect(byTest("wf-chip-agents").textContent).toContain("1 agent");
    // The PRD sign-off is a gate (its capability is_gate), Deploy is forced to ask.
    expect(byTest("wf-chip-approvals").textContent).toBe("2 approvals");
    expect(byTest("wf-chip-approvals").dataset.tip).toBe("PRD approval, Deploy");
    expect(byTest("wf-chip-inputs").textContent).toBe("2 inputs");
    expect(byTest("wf-chip-inputs").dataset.tip).toBe("Asked for when someone starts a run");
    expect(byTest("wf-starts-with").textContent).toContain("Repo");
  });

  it("Check reports the issues without writing, and the issues chip goes to the first bad step", async () => {
    api.problems = [{ step_id: "build", field: "repo", message: "Missing input repo" }];
    await mount();
    click(byTest("wf-check"));
    await settle();
    expect(api.writes.some((w) => w.action === "check")).toBe(true);
    expect(nonChecks()).toEqual([]);
    expect(byTest("wf-chip-issues").textContent).toBe("1 issue");
    expect(node("build").dataset.problem).toBe("true");
    click(node("research"));
    click(byTest("wf-chip-issues"));
    expect(node("build").dataset.selected).toBe("true");
  });
});

describe("adding, moving and removing steps", () => {
  it("adds a palette row after the selected step, selects it and opens the inspector on it", async () => {
    await mount();
    click(node("meeting"));
    click(byTest("wf-pal-agent-task"));
    const ids = laneIds();
    expect(ids).toHaveLength(5);
    const added = ids[2]!;
    expect(ids.filter((id) => id !== added)).toEqual(["research", "meeting", "spec_review", "build"]);
    expect(node(added).dataset.selected).toBe("true");
    expect(byTest("wf-inspector")).not.toBeNull();
    expect(node(added).querySelector(".wf-node-title")!.textContent).not.toBe(added);
  });

  it("adds at the end when nothing is selected", async () => {
    await mount();
    click(byTest("wf-pal-cap:notify"));
    expect(laneIds().at(-1)).toBe("notify");
  });

  it("adds at a connector's + through QuickAdd, which shows at most six", async () => {
    await mount();
    click(byTest("wf-gap-add-1"));
    expect(byTest("wf-quick-add").querySelectorAll('[role="option"]').length).toBeLessThanOrEqual(6);
    click(byTest("wf-quick-block:decision"));
    const ids = laneIds();
    expect(ids[0]).toBe("research");
    expect(ids[2]).toBe("meeting");
    expect(node(ids[1]!).dataset.type).toBe("gate");
    expect(byTest("wf-quick-add")).toBeNull();
  });

  it("finds a step in QuickAdd by typing, and adds it with Enter", async () => {
    await mount();
    click(byTest("wf-gap-add--1"));
    const input = q<HTMLInputElement>('[data-testid="wf-quick-add"] input');
    type(input, "smoke");
    key(input, "Enter");
    expect(laneIds().at(-1)).toBe("smoke_test");
  });

  it("moves the focused step with Alt+→", async () => {
    await mount();
    key(node("research"), "ArrowRight", { altKey: true });
    expect(laneIds()).toEqual(["meeting", "research", "spec_review", "build"]);
  });

  it("removes the focused step with Delete, says so, and Undo brings it back", async () => {
    await mount();
    act(() => node("meeting").focus());
    key(node("meeting"), "Delete");
    expect(laneIds()).toEqual(["research", "spec_review", "build"]);
    expect(byTest("wf-toast").textContent).toContain("Step removed");
    click(byTest("wf-toast-undo"));
    expect(laneIds()).toEqual(["research", "meeting", "spec_review", "build"]);
  });

  it("undoes and redoes with the keyboard", async () => {
    await mount();
    click(byTest("wf-pal-cap:notify"));
    act(() => (document.activeElement as HTMLElement | null)?.blur());
    key(document.body, "z", { ctrlKey: true });
    expect(laneIds()).not.toContain("notify");
    key(document.body, "z", { ctrlKey: true, shiftKey: true });
    expect(laneIds()).toContain("notify");
  });

  it("leaves Backspace to a text field", async () => {
    await mount();
    click(node("meeting"));
    key(byTest("wf-name"), "Backspace");
    expect(laneIds()).toHaveLength(4);
  });
});

describe("dragging", () => {
  function pointer(type: string, target: EventTarget, x: number, y: number) {
    act(() => {
      target.dispatchEvent(new PointerEvent(type, { clientX: x, clientY: y, pointerId: 1, button: 0,
                                                    pointerType: "mouse", bubbles: true, cancelable: true }));
    });
  }
  beforeEach(() => {
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function rect(this: Element) {
      const lane = (this as HTMLElement).classList.contains("wf-lane-viewport")
        || (this as HTMLElement).classList.contains("wf-lane-canvas");
      const r = lane ? { left: 0, top: 0, width: 4000, height: 200 } : { left: 0, top: 0, width: 100, height: 30 };
      return { ...r, x: r.left, y: r.top, right: r.left + r.width, bottom: r.top + r.height, toJSON() { return r; } } as DOMRect;
    });
  });
  afterEach(() => { vi.restoreAllMocks(); });

  it("a palette row dropped on a gap lands there, wired and selected", async () => {
    await mount();
    // The gap before `spec_review` (index 2), where the lane draws it.
    const gap = q('.wf-gap[data-at="2"]');
    const x = parseFloat(gap.style.left);
    const y = parseFloat(gap.style.top);
    pointer("pointerdown", byTest("wf-pal-cap:review"), 10, 10);
    pointer("pointermove", document.body, x, y);
    pointer("pointerup", document.body, x, y);
    const ids = laneIds();
    expect(ids).toHaveLength(5);
    expect(ids[2]).toMatch(/^review/);
    expect(node(ids[2]!).dataset.selected).toBe("true");
  });

  it("a step dragged onto another gap moves there, in one undo step", async () => {
    await mount();
    const from = q('.wf-slot[data-slot="research"]');
    pointer("pointerdown", node("research"), parseFloat(from.style.left) + 10, 40);
    pointer("pointermove", document.body, parseFloat(from.style.left) + 30, 40);
    // With research lifted, the gap after `spec_review` is final position 2.
    const target = q('.wf-slot[data-slot="build"]');
    pointer("pointermove", document.body, parseFloat(target.style.left) - 120 - 30, 40);
    pointer("pointerup", document.body, parseFloat(target.style.left) - 120 - 30, 40);
    expect(laneIds()).toEqual(["meeting", "spec_review", "research", "build"]);
    act(() => (document.activeElement as HTMLElement | null)?.blur());
    key(document.body, "z", { ctrlKey: true });
    expect(laneIds()).toEqual(["research", "meeting", "spec_review", "build"]);
  });
});

describe("saving", () => {
  it("Ctrl+S on your own workflow checks, then publishes once", async () => {
    await mount("my_flow");
    click(byTest("wf-pal-cap:notify"));
    key(byTest("wf-name"), "s", { ctrlKey: true });
    await settle();
    expect(api.writes.some((w) => w.action === "check")).toBe(true);
    expect(publishes()).toHaveLength(1);
    expect(publishes()[0]).toMatchObject({ definition_id: "wfd_own", name: "My flow" });
    expect(byTest("wf-version").textContent).toBe("v4");
    expect(byTest("wf-dirty")).toBeNull();
  });

  it("No changes: Save is unavailable on your own unchanged workflow", async () => {
    await mount("my_flow");
    expect(byTest("wf-save").getAttribute("aria-disabled")).toBe("true");
    expect(byTest("wf-save").dataset.tip).toBe("No changes");
    click(byTest("wf-save"));
    await settle();
    expect(publishes()).toEqual([]);
  });

  it("a new workflow: Blank → first step → Save creates it once and moves the URL to its key", async () => {
    await mount("new");
    expect(byTest("wf-save").getAttribute("aria-disabled")).toBe("true");
    expect(byTest("wf-save").dataset.tip).toBe("Add a step first");
    click(byTest("wf-add-first"));
    click(byTest("wf-quick-cap:research"));
    type(byTest("wf-name") as HTMLInputElement, "Lead triage");
    click(byTest("wf-save"));
    await settle();
    expect(publishes()).toHaveLength(1);
    expect(publishes()[0]).toMatchObject({ definition_id: null, name: "Lead triage" });
    expect(new URLSearchParams(window.location.search).get("wf")).toBe("lead_triage");
  });
});

describe("a new workflow", () => {
  it("offers three ways in", async () => {
    await mount("new");
    expect(byTest("wf-empty").textContent).toContain("Describe it");
    expect(byTest("wf-empty").textContent).toContain("Start from a standard");
    expect(byTest("wf-empty").textContent).toContain("Blank");
    expect(byTest("wf-empty").textContent).toContain("+ Add first step");
  });

  it("starts from a standard: its steps, named (mine), and nothing written", async () => {
    await mount("new");
    click(byTest("wf-tile-standard"));
    await settle();
    expect(byTest("wf-standard-my_flow")).toBeNull();
    click(byTest("wf-standard-standard_product_delivery"));
    await settle();
    expect(laneIds()).toEqual(["research", "meeting", "spec_review", "build"]);
    expect((byTest("wf-name") as HTMLInputElement).value).toBe("Standard Product Delivery (mine)");
    expect(nonChecks()).toEqual([]);
  });

  it("drafts steps from a description, writing nothing", async () => {
    api.draft = { name: "Clip and post", spec: linear(3) };
    await mount("new");
    click(byTest("wf-tile-describe"));
    type(q<HTMLTextAreaElement>(".wf-tile textarea"), "Trim a clip and post it");
    click(byTest("wf-draft-steps"));
    await settle();
    expect(api.writes.find((w) => w.action === "draft")).toMatchObject({ description: "Trim a clip and post it" });
    expect(laneIds()).toHaveLength(3);
    expect((byTest("wf-name") as HTMLInputElement).value).toBe("Clip and post");
    expect(publishes()).toEqual([]);
  });

  it("says Describe needs a model when there is none", async () => {
    await mount("new");
    click(byTest("wf-tile-describe"));
    type(q<HTMLTextAreaElement>(".wf-tile textarea"), "anything");
    click(byTest("wf-draft-steps"));
    await settle();
    expect(byTest("wf-tile-describe").hasAttribute("disabled")).toBe(true);
    expect(byTest("wf-tile-describe").dataset.tip).toBe(NO_MODEL_TIP);
  });
});

describe("Visual and JSON", () => {
  it("keeps the step and the view in the URL", async () => {
    await mount();
    click(node("build"));
    expect(new URLSearchParams(window.location.search).get("step")).toBe("build");
    click(byTest("wf-view-json"));
    expect(byTest("wf-json")).not.toBeNull();
    expect(new URLSearchParams(window.location.search).get("view")).toBe("json");
  });

  it("opens on the step the URL names", async () => {
    window.history.replaceState(null, "", "/?area=delivery_workflows&wf=standard_product_delivery&step=build");
    await mount();
    expect(node("build").dataset.selected).toBe("true");
  });

  it("will not leave broken JSON: Visual and Save say which line to fix", async () => {
    await mount();
    click(byTest("wf-view-json"));
    type(q<HTMLTextAreaElement>('[data-testid="wf-json"] textarea'), "{\n  \"steps\": [\n}");
    await settle(2);
    expect(byTest("wf-view-visual").getAttribute("aria-disabled")).toBe("true");
    expect(byTest("wf-view-visual").dataset.tip).toMatch(/^Fix the JSON first \(line \d+\)$/);
    expect(byTest("wf-save").getAttribute("aria-disabled")).toBe("true");
    click(byTest("wf-view-visual"));
    expect(byTest("wf-json")).not.toBeNull();
  });
});

describe("versions", () => {
  it("opens an older version read-only, and Restore makes it the draft", async () => {
    api.workflows[1].versions = [{ version: 2, spec: linear(2) }, { version: 3, spec: linear(4) }];
    await mount("my_flow");
    click(byTest("wf-version"));
    await settle();
    click(byTest("wf-open-v2"));
    await settle();
    expect(byTest("wf-viewing").textContent).toContain("Viewing v2");
    expect(laneIds()).toEqual(["research", "meeting"]);
    expect(byTest("wf-save").getAttribute("aria-disabled")).toBe("true");
    click(byTest("wf-restore"));
    await settle();
    expect(byTest("wf-viewing")).toBeNull();
    expect(byTest("wf-version").textContent).toBe("v3");
    expect(laneIds()).toEqual(["research", "meeting"]);
    expect(byTest("wf-dirty")).not.toBeNull();
  });
});

describe("starting and leaving", () => {
  it("starts a saved workflow straight away", async () => {
    await mount("my_flow");
    click(byTest("wf-start"));
    expect(onStart).toHaveBeenCalledWith("my_flow");
  });

  it("asks Save first? when there are unsaved changes", async () => {
    await mount("my_flow");
    click(byTest("wf-pal-cap:notify"));
    click(byTest("wf-start"));
    expect(byTest("wf-start-pop").textContent).toContain("Save first?");
    expect(onStart).not.toHaveBeenCalled();
  });

  it("guards unsaved work before leaving", async () => {
    await mount("my_flow");
    click(byTest("wf-back"));
    expect(onBack).toHaveBeenCalledTimes(1);
    click(byTest("wf-pal-cap:notify"));
    click(byTest("wf-back"));
    expect(onBack).toHaveBeenCalledTimes(1);
    expect(byTest("wf-leave-confirm").textContent).toContain("Discard changes?");
    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);
  });

  it("will not delete a standard, and says why", async () => {
    await mount();
    click(byTest("wf-more"));
    expect(byTest("wf-menu-delete").getAttribute("aria-disabled")).toBe("true");
    expect(byTest("wf-menu-delete").dataset.tip).toBe("Standards can't be deleted.");
  });

  it("deletes your own workflow after a confirm", async () => {
    await mount("my_flow");
    click(byTest("wf-more"));
    click(byTest("wf-menu-delete"));
    expect(byTest("wf-menu").textContent).toContain("Delete “My flow”?");
    click(byTest("wf-delete-confirm"));
    await settle();
    expect(api.writes.filter((w) => w.method === "DELETE")).toEqual([
      expect.objectContaining({ definition_id: "wfd_own" })]);
    expect(onBack).toHaveBeenCalled();
  });

  it("Duplicate opens an unsaved copy and writes nothing until Save", async () => {
    await mount("my_flow");
    click(byTest("wf-more"));
    click(byTest("wf-menu-duplicate"));
    await settle();
    expect((byTest("wf-name") as HTMLInputElement).value).toBe("My flow (copy)");
    expect(laneIds()).toEqual(["research", "meeting", "spec_review", "build"]);
    expect(new URLSearchParams(window.location.search).get("wf")).toBe("new");
    expect(nonChecks()).toEqual([]);
    // One load, not an edit and then a rename: there is nothing to undo into.
    expect(byTest("wf-undo").getAttribute("aria-disabled")).toBe("true");
  });

  it("will not Duplicate a workflow that has never been saved, and says why", async () => {
    await mount("new");
    click(byTest("wf-more"));
    const dup = byTest("wf-menu-duplicate");
    expect(dup.getAttribute("aria-disabled")).toBe("true");
    expect(dup.dataset.tip).toBe("Not saved yet");
    click(dup);
    await settle();
    expect((byTest("wf-name") as HTMLInputElement).value).toBe("Untitled workflow");
    expect(nonChecks()).toEqual([]);
  });
});

describe("Changes", () => {
  it("⋯ > Changes ends in the JSON view with the diff against the published version open", async () => {
    await mount("my_flow");
    key(node("research"), "ArrowRight", { altKey: true });
    click(byTest("wf-more"));
    click(byTest("wf-menu-changes"));
    await settle();
    expect(byTest("wf-view-json").getAttribute("aria-pressed")).toBe("true");
    expect(byTest("wf-json-changes").getAttribute("aria-pressed")).toBe("true");
    const diff = byTest("wf-json-diff");
    expect(diff).not.toBeNull();
    expect(diff.querySelector('.wf-diff-line[data-kind="add"]')).not.toBeNull();
  });
});

describe("the inspector never hides the flow", () => {
  it("under 1280px it floats over the lane, which keeps its density and scale and brings the selected step clear of it", async () => {
    api.workflows[0].spec = linear(11);
    bodyWidth = 1200 - 84 - 32;
    // The palette stays open (240px); the grid gap and the lane's border come off.
    laneWidth = bodyWidth - 240 - 12 - 2;
    const laneLeft = bodyWidth - laneWidth;
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function rect(this: Element) {
      const el = this as HTMLElement;
      const r = el.classList.contains("wf-drawer") ? { left: bodyWidth - 380, top: 0, width: 380, height: 640 }
        : el.classList.contains("wf-lane-viewport") ? { left: laneLeft, top: 40, width: laneWidth, height: 160 }
          : { left: 0, top: 0, width: 0, height: 0 };
      return { ...r, x: r.left, y: r.top, right: r.left + r.width, bottom: r.top + r.height, toJSON() { return r; } } as DOMRect;
    });
    try {
      await mount();
      const lane = q('.wf-stage [data-testid="wf-lane"]');
      const canvas = q(".wf-stage .wf-lane-canvas");
      const before = { density: lane.dataset.density, scale: canvas.dataset.scale };

      const last = laneIds().at(-1)!;
      click(node(last));
      await settle(2);
      expect(byTest("wf-drawer")).not.toBeNull();
      expect(lane.dataset.density).toBe(before.density);
      expect(canvas.dataset.scale).toBe(before.scale);
      // Brought out from under the drawer, which covers the lane's last 380px.
      const viewport = q(".wf-stage .wf-lane-viewport");
      const box = q(`.wf-slot[data-slot="${last}"]`);
      const right = (parseFloat(box.style.left) + parseFloat(box.style.width)) * Number(before.scale) - viewport.scrollLeft;
      expect(right).toBeLessThanOrEqual(laneWidth - 380);

      click(q(".wf-drawer [aria-label='Close']"));
      await settle(2);
      expect(lane.dataset.density).toBe(before.density);
      expect(canvas.dataset.scale).toBe(before.scale);
    } finally {
      vi.restoreAllMocks();
    }
  });

  it("from 1280px gives the drawer a column beside the lane in both views, so the lane refits instead of being covered", () => {
    const css = readFileSync("app/workflows/editor.css", "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    const wide = css.slice(css.indexOf("@media (min-width: 1280px)"));
    const block = wide.slice(0, wide.indexOf("\n}") + 2);
    // Visual, with the palette open and folded.
    expect(block).toMatch(/\.wf-editor\[data-inspector="open"\] \.wf-body \{ grid-template-columns: 240px minmax\(0, 1fr\) 380px;/);
    expect(block).toMatch(/\.wf-editor\[data-inspector="open"\] \.wf-body\[data-palette="rail"\] \{ grid-template-columns: 48px minmax\(0, 1fr\) 380px;/);
    expect(block).toMatch(/\[data-view="json"\] \.wf-body \{ grid-template-columns: minmax\(0, 1fr\) 380px;/);
    // In its column, not floating over the lane, in either view.
    expect(block).toMatch(/\n  \.wf-drawer \{ position: sticky;/);
    // Under 1280px it floats.
    expect(css.slice(0, css.indexOf("@media"))).toMatch(/\.wf-drawer \{\s*position: absolute;/);
  });

  it("from 1280px the drawer sits beside the lane: the lane gets no room to scroll under it", async () => {
    api.workflows[0].spec = linear(4);
    bodyWidth = 1440 - 84 - 32;
    laneWidth = bodyWidth - 240 - 12 - 380 - 12 - 2;
    const laneLeft = 252;
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function rect(this: Element) {
      const el = this as HTMLElement;
      const r = el.classList.contains("wf-drawer") ? { left: laneLeft + laneWidth + 14, top: 0, width: 380, height: 640 }
        : el.classList.contains("wf-lane-viewport") ? { left: laneLeft, top: 40, width: laneWidth, height: 160 }
          : { left: 0, top: 0, width: 0, height: 0 };
      return { ...r, x: r.left, y: r.top, right: r.left + r.width, bottom: r.top + r.height, toJSON() { return r; } } as DOMRect;
    });
    try {
      await mount();
      const sizer = q(".wf-stage .wf-lane-sizer");
      const before = sizer.style.width;
      click(node(laneIds().at(-1)!));
      await settle(2);
      expect(byTest("wf-drawer")).not.toBeNull();
      expect(sizer.style.width).toBe(before);
    } finally {
      vi.restoreAllMocks();
    }
  });
});

describe("the whole flow in view", () => {
  it("folds the palette to its rail when that is what lets an 11-step flow fit at 1440px", async () => {
    api.workflows[0].spec = linear(11);
    bodyWidth = 1440 - 84 - 32;
    await mount();
    expect(byTest("wf-palette").dataset.collapsed).toBe("true");
  });

  it("keeps the palette open when the flow fits anyway", async () => {
    bodyWidth = 1440 - 84 - 32;
    await mount();
    expect(byTest("wf-palette").dataset.collapsed).toBeUndefined();
  });

  it("remembers the operator's own choice", async () => {
    api.workflows[0].spec = linear(11);
    bodyWidth = 1440 - 84 - 32;
    await mount();
    click(q(".wf-pal-toggle"));
    expect(byTest("wf-palette").dataset.collapsed).toBeUndefined();
    expect(window.localStorage.getItem("wf:palette")).toBe("open");
  });
});

describe("copy budget", () => {
  it("prints no sentence in the editor: six words at most outside a tooltip, and no paragraphs", async () => {
    api.workflows[0].spec = { ...linear(10, (i) => (i === 3 ? { captain: "qa" } : {})),
                              input_schema: { required: ["repo"], properties: { repo: {} } } };
    api.problems = [{ step_id: "build", field: null, message: "This step needs an input that nothing upstream makes" }];
    await mount();
    click(byTest("wf-check"));
    await settle();
    click(node("build"));
    click(byTest("wf-more"));
    expect(overBudget(document.body)).toEqual([]);
    expect(paragraphs(document.body)).toEqual([]);
  });

  it("holds on the new-workflow screen and in the JSON view", async () => {
    await mount("new");
    click(byTest("wf-tile-describe"));
    expect(overBudget(document.body)).toEqual([]);
    click(byTest("wf-view-json"));
    expect(overBudget(document.body)).toEqual([]);
    expect(paragraphs(document.body)).toEqual([]);
  });
});
