import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Schedule } from "../app/workflows/library";
import type { WorkflowSummary } from "../lib/workflow-editor-types";

/**
 * The collapsed card must say paused when a schedule is off. That word is on the
 * card, not only in the schedule tooltip. Every schedule on means the word is absent.
 * The last run's status stays beside its age.
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

const schedule = (over: Pick<Schedule, "workflow_key" | "enabled"> & Partial<Schedule>): Schedule => ({
  id: `sch_${over.workflow_key}_${over.enabled ? "on" : "off"}`,
  name: "Weekly",
  describes: "Mondays 08:00",
  next_run_at: null,
  last_run_at: null,
  last_error: null,
  ...over,
});

const props = (schedules: Schedule[]) => ({
  projectId: "p1", busy: null, schedules, inputSchemas: {}, examples: {},
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

async function mount(schedules: Schedule[]) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root!.render(<Library {...props(schedules)} />); });
  for (let i = 0; i < 10; i += 1) {
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }
}

const card = (key: string) => host!.querySelector<HTMLElement>(`[data-testid="wf-card-${key}"]`);

const scheduled = (key: string) =>
  host!.querySelector<HTMLElement>(`[data-testid="wf-card-scheduled-${key}"]`);

/** Visible words only. A title tooltip is not text. */
function words(el: HTMLElement): string[] {
  return (el.textContent ?? "").replace(/\s+/g, " ").trim().split(" ").filter(Boolean);
}

describe("the collapsed workflow card says paused", () => {
  it("says paused when a schedule is off, and still says the last run", async () => {
    stub.fetchLibrary.mockResolvedValue([
      wf({ workflow_key: "digest", name: "Digest",
           last_run: { status: "failed", at: "2026-10-01T09:00:00Z" } }),
    ]);
    await mount([schedule({ workflow_key: "digest", enabled: false })]);

    const box = card("digest");
    const when = scheduled("digest");
    const run = box?.querySelector<HTMLElement>("span[title^='Last run ']") ?? null;
    expect(box).not.toBeNull();
    expect(when).not.toBeNull();
    expect(when!.querySelector("svg")).not.toBeNull();
    expect(run).not.toBeNull();
    expect(words(run!)).toContain("failed");
    expect(words(when!)).toContain("paused");
  });

  it("does not say paused when every schedule is on", async () => {
    stub.fetchLibrary.mockResolvedValue([
      wf({ workflow_key: "notes", name: "Notes",
           last_run: { status: "completed", at: "2026-10-02T15:30:00Z" } }),
    ]);
    await mount([
      schedule({ id: "sch_notes_a", workflow_key: "notes", enabled: true }),
      schedule({ id: "sch_notes_b", workflow_key: "notes", enabled: true, describes: "Fridays 16:00" }),
    ]);

    const box = card("notes");
    const when = scheduled("notes");
    const run = box?.querySelector<HTMLElement>("span[title^='Last run ']") ?? null;
    expect(box).not.toBeNull();
    expect(when).not.toBeNull();
    expect(when!.querySelector("svg")).not.toBeNull();
    expect(words(when!)).not.toContain("paused");
    expect(words(box!)).not.toContain("paused");
    expect(run).not.toBeNull();
    expect(words(run!)).toContain("completed");
  });
});
