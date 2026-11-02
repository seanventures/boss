// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkflowSummary } from "../lib/workflow-editor-types";
import { cssOnly } from "./_helpers/source";

/**
 * The Library, mounted and pressed (Workflow Studio spec 2.2 and 3.6).
 *
 * One list, one card design, yours first; copies piled behind one source collapse onto it;
 * Delete is Retire when a workflow has runs and says "Has live runs" when the API refuses; a
 * bulk delete; the Steps view; and nothing written except by Delete and Rename.
 */
vi.mock("../app/workflows/studio-parts", () => import("./_helpers/studio-parts-stub"));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { Library } = await import("../app/workflows/library");
const stub = await import("./_helpers/studio-parts-stub");

const wf = (over: Partial<WorkflowSummary>): WorkflowSummary => ({
  definition_id: `def_${over.workflow_key}`, workflow_key: "k", name: "K", is_catalogue: false,
  version: 1, step_count: 2, run_count: 0,
  steps: [{ id: "a", title: "Plan", captain: "pm" }, { id: "b", title: "Ship", approval: "manual" }],
  ...over,
});

const LIBRARY: WorkflowSummary[] = [
  wf({ workflow_key: "spd", name: "Standard Product Delivery", is_catalogue: true, version: 4 }),
  wf({ workflow_key: "spd_copy", name: "SPD (copy)", forked_from_workflow_key: "spd",
       forked_from_name: "Standard Product Delivery" }),
  wf({ workflow_key: "spd_copy_2", name: "SPD (copy) (copy)", forked_from_workflow_key: "spd",
       forked_from_name: "Standard Product Delivery", run_count: 3 }),
  wf({ workflow_key: "video", name: "Family video", description: "Cut and publish a family video",
       last_run: { status: "completed", at: "2026-10-01T09:00:00Z" } }),
  wf({ workflow_key: "busy", name: "Busy one", run_count: 2 }),
];

const props = () => ({
  projectId: "p1", busy: null, schedules: [], inputSchemas: {}, examples: {},
  workClasses: ["project.new"], riskFloor: 2,
  customSteps: [{
    step_key: "file_issue", label: "File an issue", summary: "Opens a ticket",
    binding_kind: "mcp_tool" as const, skill_id: null, mcp_tool_name: "create_issue",
    inputs: [{ name: "title", required: true, description: "" }], produces: ["issue_url"],
    enabled: true, risk_tier: 1, risk_source: "mcp", trusted: true, usable: true,
    unusable_reason: null, approval_forced_manual: false,
  }],
  onStart: vi.fn(), onOpen: vi.fn(), onNew: vi.fn(), onDuplicate: vi.fn(), onUseFor: vi.fn(),
  onSaveSchedule: vi.fn(async () => true), onToggleSchedule: vi.fn(), onDeleteSchedule: vi.fn(),
  onRemoveStep: vi.fn(), onStepsChanged: vi.fn(),
});

let root: Root | null = null;
let host: HTMLElement | null = null;
let p = props();

beforeEach(() => {
  window.history.replaceState(null, "", "/?area=delivery_workflows&tab=library");
  stub.fetchLibrary.mockReset().mockResolvedValue(LIBRARY);
  stub.deleteWorkflow.mockReset().mockResolvedValue({ outcome: "deleted" });
  stub.renameWorkflow.mockReset().mockResolvedValue(undefined);
  p = props();
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
});

async function settle() {
  for (let i = 0; i < 10; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

async function mount() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<Library {...p} />));
  await settle();
}

const $ = (sel: string) => host!.querySelector<HTMLElement>(sel);
const $$ = (sel: string) => [...host!.querySelectorAll<HTMLElement>(sel)];
const click = async (el: HTMLElement | null) => {
  expect(el).not.toBeNull();
  await act(async () => { el!.click(); });
  await settle();
};
const menuItem = (label: string) => $$('[role="menuitem"]').find((b) => b.textContent === label) ?? null;

describe("one list, yours first", () => {
  it("draws your workflows, then a Standards divider, then the standards", async () => {
    await mount();
    const yours = $$('[data-testid="wf-lib-yours"] > article').map((c) => c.dataset.testid);
    expect(yours).toEqual(["wf-card-busy", "wf-card-video"]);
    const html = host!.innerHTML;
    expect(html.indexOf('data-testid="wf-lib-standards"')).toBeGreaterThan(html.indexOf("wf-card-video"));
    expect($('[data-testid="wf-card-spd"]')).not.toBeNull();
  });

  it("collapses the copies of one source behind +2 copies on the source card", async () => {
    await mount();
    // Not loose in Yours…
    expect($('[data-testid="wf-lib-yours"] [data-testid="wf-card-spd_copy"]')).toBeNull();
    const more = $('[data-testid="wf-card-copies-spd"]');
    expect(more?.textContent).toBe("+2 copies");
    // …and there, with the source named, when opened.
    await click(more);
    const list = $('[data-testid="wf-card-copylist-spd"]');
    expect(list?.querySelector('[data-testid="wf-card-spd_copy"]')).not.toBeNull();
    expect(list?.textContent).toContain("from Standard Product Delivery");
  });

  it("finds by name, description or step, and says when nothing matches", async () => {
    await mount();
    const search = $('[data-testid="wf-lib-search"]') as HTMLInputElement;
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(search, "publish");
      search.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await settle();
    expect($$("article[data-testid^=wf-card-]").map((c) => c.dataset.testid)).toEqual(["wf-card-video"]);
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(search, "nothing like this");
      search.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await settle();
    expect($('[data-testid="wf-lib-no-match"]')).not.toBeNull();
  });

  it("says plainly when you have none of your own", async () => {
    stub.fetchLibrary.mockResolvedValue([LIBRARY[0]]);
    await mount();
    expect($('[data-testid="wf-lib-yours-empty"]')?.textContent).toContain("No workflows of your own yet");
  });
});

describe("a card's presses", () => {
  it("clicking the card opens it; Start starts it; neither writes", async () => {
    await mount();
    await click($('[data-testid="wf-card-video"]'));
    expect(p.onOpen).toHaveBeenCalledWith("video");
    await click($('[data-testid="wf-card-start-video"]'));
    expect(p.onStart).toHaveBeenCalledWith("video");
    expect(p.onOpen).toHaveBeenCalledTimes(1); // Start did not also open the card
    expect(stub.deleteWorkflow).not.toHaveBeenCalled();
  });

  it("Delete asks first, then deletes by definition id", async () => {
    await mount();
    await click($('[data-testid="wf-card-more-video"]'));
    await click(menuItem("Delete"));
    expect($('[data-testid="wf-card-confirm-video"]')?.textContent).toContain("Delete “Family video”?");
    expect(stub.deleteWorkflow).not.toHaveBeenCalled();
    await click($('[data-testid="wf-card-confirm-delete-video"]'));
    expect(stub.deleteWorkflow).toHaveBeenCalledTimes(1);
    expect(stub.deleteWorkflow).toHaveBeenCalledWith("p1", "def_video");
  });

  it("is Retire when the workflow has runs", async () => {
    await mount();
    await click($('[data-testid="wf-card-more-busy"]'));
    expect(menuItem("Retire")).not.toBeNull();
    expect(menuItem("Delete")).toBeNull();
    await click(menuItem("Retire"));
    expect($('[data-testid="wf-card-confirm-busy"]')?.textContent).toContain("Retire “Busy one”?");
  });

  it("a refusal for live runs says so on the card", async () => {
    stub.deleteWorkflow.mockRejectedValue(Object.assign(new Error("Has live runs"), { status: 409 }));
    await mount();
    await click($('[data-testid="wf-card-more-busy"]'));
    await click(menuItem("Retire"));
    await click($('[data-testid="wf-card-confirm-delete-busy"]'));
    expect($('[data-testid="wf-card-live-busy"]')?.textContent).toBe("Has live runs");
  });

  it("a standard offers Try example and Use for, and cannot be deleted", async () => {
    p.examples = { spd: { title: "Shift swap", directive: "Build a shift swap board" } };
    await mount();
    await click($('[data-testid="wf-card-more-spd"]'));
    const del = menuItem("Delete") as HTMLButtonElement;
    expect(del.disabled).toBe(true);
    expect(del.title).toBe("Standards can't be deleted.");
    await click($('[data-testid="wf-card-example-spd"]'));
    expect(p.onStart).toHaveBeenCalledWith("spd", { title: "Shift swap", directive: "Build a shift swap board" });
    // No checkbox on a standard: bulk delete cannot reach one.
    expect($('[data-testid="wf-card-select-spd"]')).toBeNull();
  });

  it("Rename writes the name, and a clash says Name in use", async () => {
    stub.renameWorkflow.mockRejectedValueOnce(new stub.NameInUseError("Family video 2"));
    await mount();
    await click($('[data-testid="wf-card-more-video"]'));
    await click(menuItem("Rename"));
    const input = $('[data-testid="wf-card-name-input-video"]') as HTMLInputElement;
    const type = async (value: string) => act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await type("Busy one");
    await act(async () => { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    await settle();
    expect(stub.renameWorkflow).toHaveBeenCalledWith("p1", "def_video", { name: "Busy one" });
    expect(host!.textContent).toContain("Name in use");
    await type("Family video (cut)");
    await act(async () => { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    await settle();
    expect(stub.renameWorkflow).toHaveBeenLastCalledWith("p1", "def_video", { name: "Family video (cut)" });
    expect($('[data-testid="wf-card-name-input-video"]')).toBeNull();
  });
});

/**
 * Dismissing is not opening, and an open popover stays on screen (review of ws/ia, 2026-10-03).
 *
 * A real press is pointerdown then click: the outside pointerdown closed the menu before the
 * click reached the card, so clicking the card body to dismiss the ⋯ menu opened the editor.
 * And the popovers live in the hover-revealed actions, which faded out once the menu button that
 * held focus was unmounted by picking Schedule…, Use for… or Delete.
 */
describe("a card's popovers", () => {
  const press = async (el: HTMLElement | null) => {
    expect(el).not.toBeNull();
    // Two acts, as a browser does it: React commits the discrete pointerdown's update before
    // the click is dispatched.
    await act(async () => { el!.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true })); });
    await act(async () => { el!.click(); });
    await settle();
  };

  it("a press on the card body that dismisses the menu does not open the editor", async () => {
    await mount();
    await click($('[data-testid="wf-card-more-video"]'));
    expect($('[data-testid="wf-card-menu-video"]')).not.toBeNull();
    await press($('[data-testid="wf-card-video"] .wf-card-desc'));
    expect($('[data-testid="wf-card-menu-video"]')).toBeNull();
    expect(p.onOpen).not.toHaveBeenCalled();
    // The next press is an ordinary one and opens it.
    await press($('[data-testid="wf-card-video"] .wf-card-desc'));
    expect(p.onOpen).toHaveBeenCalledWith("video");
  });

  it("a press on the card body that dismisses the rename field does not open the editor", async () => {
    await mount();
    await click($('[data-testid="wf-card-more-video"]'));
    await click(menuItem("Rename"));
    const input = $('[data-testid="wf-card-name-input-video"]') as HTMLInputElement;
    const desc = $('[data-testid="wf-card-video"] .wf-card-desc')!;
    // The browser's own default for a pointerdown elsewhere: the field loses focus, which saves
    // and closes it, before the click.
    await act(async () => {
      desc.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
      input.blur();
    });
    await settle();
    expect($('[data-testid="wf-card-name-input-video"]')).toBeNull();
    await act(async () => { desc.click(); });
    await settle();
    expect(p.onOpen).not.toHaveBeenCalled();
  });

  it("an open popover keeps the card's actions shown and takes focus", async () => {
    await mount();
    const actions = () => $('[data-testid="wf-card-video"] > .wf-card-actions');
    expect(actions()?.hasAttribute("data-open")).toBe(false);
    await click($('[data-testid="wf-card-more-video"]'));
    expect(actions()?.dataset.open).toBe("menu");
    expect(document.activeElement?.getAttribute("role")).toBe("menuitem");

    await click(menuItem("Delete"));
    const confirm = $('[data-testid="wf-card-confirm-video"]');
    expect(actions()?.dataset.open).toBe("delete");
    // Focus is on the confirm itself, never on its destructive button.
    expect(document.activeElement).toBe(confirm);

    await act(async () => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    await settle();
    expect(actions()?.hasAttribute("data-open")).toBe(false);
  });

  it("the stylesheet shows the actions while a popover is open", () => {
    expect(cssOnly("app/workflows/library.css")).toMatch(
      /\.wf-card-actions\[data-open\][^{]*\{\s*opacity:\s*1;/);
  });
});

describe("bulk delete", () => {
  it("selects own workflows and deletes each once, keeping the ones refused", async () => {
    stub.deleteWorkflow.mockImplementation(async (_p: string, id: string) => {
      if (id === "def_busy") throw Object.assign(new Error("live runs"), { status: 409 });
      return { outcome: "deleted" as const };
    });
    await mount();
    await click($('[data-testid="wf-card-select-video"]'));
    await click($('[data-testid="wf-card-select-busy"]'));
    expect($('[data-testid="wf-lib-selbar"]')?.textContent).toContain("2 selected");
    await click($('[data-testid="wf-lib-bulk-delete"]'));
    await click($('[data-testid="wf-lib-bulk-confirm"]'));
    expect(stub.deleteWorkflow.mock.calls.map((c) => c[1]).sort()).toEqual(["def_busy", "def_video"]);
    expect($('[data-testid="wf-lib-selbar"]')?.textContent).toContain("1 selected");
    expect($('[data-testid="wf-card-live-busy"]')).not.toBeNull();
  });
});

describe("the Steps view", () => {
  it("lists your steps with what they bind to, and + New step opens the modal in place", async () => {
    await mount();
    await click($('[data-testid="wf-lib-view-steps"]'));
    expect(new URLSearchParams(window.location.search).get("lib")).toBe("steps");
    const row = $('[data-testid="wf-step-row-file_issue"]');
    expect(row?.textContent).toContain("File an issue");
    expect(row?.textContent).toContain("create_issue");
    await click($('[data-testid="wf-lib-new-step"]'));
    const modal = $('[data-testid="define-step-modal"]');
    expect(modal?.dataset.edit).toBe("");
    await click(modal!.querySelector("button"));
    expect(p.onStepsChanged).toHaveBeenCalledTimes(1);
    await click($('[data-testid="wf-step-edit-file_issue"]'));
    expect($('[data-testid="define-step-modal"]')?.dataset.edit).toBe("file_issue");
  });

  it("opens on Steps when the URL says so (an old 'Your steps' link)", async () => {
    window.history.replaceState(null, "", "/?area=delivery_workflows&tab=library&lib=steps");
    await mount();
    expect($('[data-testid="wf-lib-steps"]')).not.toBeNull();
  });
});

describe("the keyboard", () => {
  it("/ finds and N starts a new workflow, but not while typing", async () => {
    await mount();
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "/" })); });
    expect(document.activeElement).toBe($('[data-testid="wf-lib-search"]'));
    await act(async () => {
      document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: "n", bubbles: true }));
    });
    expect(p.onNew).not.toHaveBeenCalled();
    (document.activeElement as HTMLElement).blur();
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "N" })); });
    expect(p.onNew).toHaveBeenCalledTimes(1);
  });
});

describe("a workflow opens full page from its card", () => {
  it("the card's full-page button and its menu item open that workflow with the whole window", async () => {
    const onOpenFull = vi.fn();
    p = { ...props(), onOpenFull } as typeof p;
    await mount();
    await click($('[data-testid="open-full-page-spd"]'));
    expect(onOpenFull).toHaveBeenCalledWith("spd");
    expect(p.onOpen).not.toHaveBeenCalled();
    await click($('[data-testid="wf-card-more-spd"]'));
    await click(menuItem("Open full page"));
    expect(onOpenFull).toHaveBeenLastCalledWith("spd");
  });

  it("offers no full-page button where the host has no full page", async () => {
    await mount();
    expect($('[data-testid="open-full-page-spd"]')).toBeNull();
  });
});
