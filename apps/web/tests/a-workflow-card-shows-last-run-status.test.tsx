import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkflowSummary } from "../lib/workflow-editor-types";

/**
 * A library card already says how long ago the last run was. The status of that run
 * must be readable on the card, not only in the time tooltip. No run means no status.
 */
vi.mock("../app/workflows/studio-parts", () => import("./_helpers/studio-parts-stub"));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { Library } = await import("../app/workflows/library");
const stub = await import("./_helpers/studio-parts-stub");

const wf = (over: Partial<WorkflowSummary>): WorkflowSummary => ({
  definition_id: `def_${over.workflow_key}`,
  workflow_key: "k",
  name: "K",
  is_catalogue: false,
  version: 1,
  step_count: 1,
  run_count: 0,
  steps: [{ id: "a", title: "Plan" }],
  ...over,
});

const props = () => ({
  projectId: "p1", busy: null, schedules: [], inputSchemas: {}, examples: {},
  workClasses: [], customSteps: [], riskFloor: 2,
  onStart: vi.fn(), onOpen: vi.fn(), onNew: vi.fn(), onDuplicate: vi.fn(), onUseFor: vi.fn(),
  onSaveSchedule: vi.fn(async () => true), onToggleSchedule: vi.fn(), onDeleteSchedule: vi.fn(),
  onRemoveStep: vi.fn(), onStepsChanged: vi.fn(),
});

let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => {
  window.history.replaceState(null, "", "/?area=delivery_workflows&tab=library");
  stub.fetchLibrary.mockReset();
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

async function mount() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root!.render(<Library {...props()} />); });
  for (let i = 0; i < 10; i += 1) {
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }
}

const card = (key: string) => host!.querySelector<HTMLElement>(`[data-testid="wf-card-${key}"]`);

function lastRun(key: string): HTMLElement | null {
  return card(key)?.querySelector<HTMLElement>(".wf-card-meta span[title^='Last run ']") ?? null;
}

const AGE = /^(?:now|\d+(?:mo|m|h|d|w))$/;

function visibleTokens(el: HTMLElement): string[] {
  return (el.textContent ?? "").replace(/\s+/g, " ").trim().split(" ").filter(Boolean);
}

/** Visible words on the last-run control, with the age token taken off. */
function statusWords(el: HTMLElement): string[] {
  return visibleTokens(el).filter((token) => !AGE.test(token));
}

describe("a workflow card shows the last run's status", () => {
  it("prints that status beside the age and keeps the time in the title", async () => {
    stub.fetchLibrary.mockResolvedValue([
      wf({ workflow_key: "video", name: "Family video",
           last_run: { status: "failed", at: "2026-10-01T09:00:00Z" } }),
      wf({ workflow_key: "notes", name: "Quiet notes",
           last_run: { status: "completed", at: "2026-10-02T15:30:00Z" } }),
    ]);
    await mount();

    const failed = lastRun("video");
    expect(failed).not.toBeNull();
    expect(failed!.getAttribute("title")).toBe("Last run failed, Oct 1, 2026, 9:00 AM UTC");
    expect(statusWords(failed!)).toEqual(["failed"]);
    expect(visibleTokens(failed!).some((token) => AGE.test(token))).toBe(true);

    const completed = lastRun("notes");
    expect(completed).not.toBeNull();
    expect(completed!.getAttribute("title")).toBe("Last run completed, Oct 2, 2026, 3:30 PM UTC");
    expect(statusWords(completed!)).toEqual(["completed"]);
    expect(visibleTokens(completed!).some((token) => AGE.test(token))).toBe(true);

    expect(card("video")!.textContent ?? "").not.toMatch(/%|success rate/i);
    expect(card("notes")!.textContent ?? "").not.toMatch(/%|success rate/i);
  });

  it("does not invent a status when the workflow has no last run", async () => {
    stub.fetchLibrary.mockResolvedValue([
      wf({ workflow_key: "fresh", name: "Fresh", last_run: null }),
    ]);
    await mount();
    const fresh = card("fresh");
    expect(fresh).not.toBeNull();
    expect(fresh!.querySelector("[title^='Last run']")).toBeNull();
    expect(fresh!.textContent ?? "").not.toMatch(/\b(failed|completed|succeeded|running|unknown)\b/i);
    expect(fresh!.textContent ?? "").not.toMatch(/%|success rate/i);
  });
});
