// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { WorkflowSummary } from "../lib/workflow-editor-types";

/**
 * Tooltips, not paragraphs — enforced as a class (Workflow Studio spec 3.8, test 6).
 *
 *   "lots of text — make it tooltip / info, no one reads details, keep it concise"
 *
 * Each Workflow Studio surface is rendered with fixtures and every visible text node is
 * walked. A label is at most six words; anything longer belongs in a HelpTip or a title. Text
 * the operator wrote (a workflow's name, its description, a schedule's sentence) is theirs, not
 * copy, and is marked `data-user-text`. And there is no <p> inside the editor at all.
 *
 * ONE FILE FOR ALL THREE STREAMS: the Library is rendered here (S4); the editor (Visual and
 * JSON) and the inspector (agent task, built-in, gate) join it as `surface(...)` cases when
 * they land (S2, S3).
 */
vi.mock("../app/workflows/studio-parts", () => import("./_helpers/studio-parts-stub"));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const stub = await import("./_helpers/studio-parts-stub");
const { Library } = await import("../app/workflows/library");

const MAX_WORDS = 6;
const EXEMPT = "[data-user-text], [data-tip], [role=tooltip], script, style";

/** Every visible text node longer than the budget, with where it sits. */
function overBudget(root: HTMLElement): string[] {
  const out: string[] = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = (node.textContent ?? "").replace(/\s+/g, " ").trim();
    if (!text) continue;
    const parent = node.parentElement;
    if (!parent || parent.closest(EXEMPT)) continue;
    const words = text.split(" ").filter((w) => /[\p{L}\p{N}]/u.test(w));
    if (words.length > MAX_WORDS) out.push(`${parent.tagName.toLowerCase()}: "${text}"`);
  }
  return out;
}

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
});

async function surface(node: React.ReactElement, before?: (host: HTMLElement) => Promise<void>) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  for (let i = 0; i < 10; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  if (before) await before(host);
  return host;
}

const wf = (over: Partial<WorkflowSummary>): WorkflowSummary => ({
  definition_id: `def_${over.workflow_key}`, workflow_key: "k", name: "K", is_catalogue: false,
  version: 3, step_count: 2, run_count: 0, steps: [{ id: "a", title: "Plan" }], ...over,
});

const libraryProps = {
  projectId: "p1", busy: null, inputSchemas: {}, workClasses: ["project.new", "ops.incident"],
  riskFloor: 2, examples: { spd: { title: "Example", directive: "Build it" } },
  schedules: [{ id: "s1", workflow_key: "mine", name: "Mine weekly", enabled: true,
                describes: "Every Monday at 08:00 in Asia/Kolkata, with the usual sectors",
                next_run_at: null, last_run_at: null, last_error: "the model was out of credit" }],
  customSteps: [{ step_key: "file_issue", label: "File an issue in the tracker for this project",
                  summary: "A long summary that would be a paragraph if it were drawn on screen",
                  binding_kind: "skill" as const, skill_id: "tracker.file_issue", mcp_tool_name: null,
                  inputs: [], produces: [], enabled: true, risk_tier: 3, risk_source: "skill",
                  trusted: true, usable: false, unusable_reason: "Its skill was removed from the project",
                  approval_forced_manual: true }],
  uninstalledStandards: 3, onInstallStandards: vi.fn(),
  onStart: vi.fn(), onOpen: vi.fn(), onNew: vi.fn(), onDuplicate: vi.fn(), onUseFor: vi.fn(),
  onSaveSchedule: vi.fn(async () => true), onToggleSchedule: vi.fn(), onDeleteSchedule: vi.fn(),
  onRemoveStep: vi.fn(), onStepsChanged: vi.fn(),
};

const click = async (el: Element | null) => {
  expect(el).not.toBeNull();
  await act(async () => { (el as HTMLElement).click(); });
  for (let i = 0; i < 5; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
};

describe("the Library has no paragraphs", () => {
  it("the grid, with yours, copies, standards and a long description", async () => {
    stub.fetchLibrary.mockResolvedValue([
      wf({ workflow_key: "mine", name: "A rather long workflow name that the operator chose",
           description: "Cuts a family video, writes its captions and publishes it to the channel",
           run_count: 2, last_run: { status: "completed", at: "2026-10-01T00:00:00Z" } }),
      wf({ workflow_key: "spd", name: "Standard Product Delivery", is_catalogue: true }),
      wf({ workflow_key: "c1", name: "SPD (copy)", forked_from_workflow_key: "spd",
           forked_from_name: "Standard Product Delivery" }),
      wf({ workflow_key: "c2", name: "SPD (copy) (copy)", forked_from_workflow_key: "spd",
           forked_from_name: "Standard Product Delivery" }),
    ]);
    const el = await surface(<Library {...libraryProps} />, async (h) => {
      await click(h.querySelector('[data-testid="wf-card-copies-spd"]'));
      await click(h.querySelector('[data-testid="wf-card-select-mine"]'));
    });
    expect(overBudget(el)).toEqual([]);
  });

  it("every menu and confirm a card opens", async () => {
    stub.fetchLibrary.mockResolvedValue([
      wf({ workflow_key: "mine", name: "Mine", run_count: 1 }),
      wf({ workflow_key: "spd", name: "Standard Product Delivery", is_catalogue: true }),
    ]);
    for (const [key, item] of [["mine", "Retire"], ["mine", "Use for…"], ["mine", "Schedule…"],
                               ["spd", "Use for…"]] as const) {
      const el = await surface(<Library {...libraryProps} />, async (h) => {
        await click(h.querySelector(`[data-testid="wf-card-more-${key}"]`));
        expect(overBudget(h), `${key} menu`).toEqual([]);
        await click([...h.querySelectorAll('[role="menuitem"]')].find((b) => b.textContent === item) ?? null);
      });
      expect(overBudget(el), `${key} › ${item}`).toEqual([]);
      act(() => root!.unmount());
      host!.remove();
    }
    root = null; host = null;
  });

  it("the empty Library and the Steps view", async () => {
    stub.fetchLibrary.mockResolvedValue([]);
    const el = await surface(<Library {...libraryProps} />);
    expect(overBudget(el)).toEqual([]);
    await click(el.querySelector('[data-testid="wf-lib-view-steps"]'));
    expect(overBudget(el)).toEqual([]);
  });

  it("the walker itself catches a paragraph", async () => {
    const el = await surface(<div><span>Six words make a fine label</span>
      <span>but seven words is one word too many</span>
      <span title="anything in a title is fine">x</span>
      <span data-user-text>whatever the operator typed can be as long as they like</span></div>);
    expect(overBudget(el)).toEqual(['span: "but seven words is one word too many"']);
  });
});
