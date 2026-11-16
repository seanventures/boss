import { readFileSync } from "node:fs";
import { join } from "node:path";

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { scheduleMissSentence, type Schedule } from "../app/workflows/library";
import type { WorkflowSummary } from "../lib/workflow-editor-types";

/**
 * A schedule that did not start already explains itself in the row title. That
 * explanation has to be on the row as well: the last error, trimmed, or a short
 * "not started" when the firing was missing something. A sentence longer than
 * six words is a paragraph. The title and the warning icon stay.
 */
vi.mock("../app/workflows/studio-parts", () => import("./_helpers/studio-parts-stub"));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { Library } = await import("../app/workflows/library");
const stub = await import("./_helpers/studio-parts-stub");

const LONG_ERROR = "the worker lease expired";
const LONG = `Last run failed: ${LONG_ERROR}`;
const SIX_ERROR = "disk full now";
const SIX = `Last run failed: ${SIX_ERROR}`;
const SHORT = "Not started last time.";

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

const schedule = (over: Partial<Schedule> & Pick<Schedule, "id">): Schedule => ({
  workflow_key: "digest",
  name: "Weekly",
  describes: "Mondays 08:00",
  enabled: true,
  next_run_at: "2026-11-16T08:00:00Z",
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
  scheduleFor: "digest",
});

let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => {
  window.history.replaceState(null, "", "/?area=delivery_workflows&tab=library");
  stub.fetchLibrary.mockReset();
  vi.stubGlobal("fetch", vi.fn(async () => new Response("", { status: 404 })));
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  vi.unstubAllGlobals();
});

async function mount(schedules: Schedule[]) {
  stub.fetchLibrary.mockResolvedValue([
    wf({ workflow_key: "digest", name: "Digest" }),
  ]);
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root!.render(<Library {...props(schedules)} />); });
  for (let i = 0; i < 10; i += 1) {
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }
}

function row(id: string): HTMLElement {
  const li = host!.querySelector(`[data-testid="wf-schedule-toggle-${id}"]`)?.closest("li");
  expect(li).not.toBeNull();
  return li as HTMLElement;
}

function titled(li: HTMLElement): HTMLElement {
  const span = li.querySelector("span[title]");
  expect(span).not.toBeNull();
  return span as HTMLElement;
}

describe("the schedule row says why it did not start", () => {
  it("says the last run failed, using the trimmed error, even when something was also missing", () => {
    expect(scheduleMissSentence({
      last_error: `  ${LONG_ERROR}  `,
      last_missing: [{ key: "host", label: "A deploy host", fix: "Add a host" }],
    })).toBe(LONG);
  });

  it("says it was not started when the error is blank and something was missing", () => {
    expect(scheduleMissSentence({
      last_error: "   ",
      last_missing: [{ key: "host", label: "A deploy host" }],
    })).toBe(SHORT);
    expect(scheduleMissSentence({
      last_error: null,
      last_missing: [{ key: "host", label: "A deploy host" }],
    })).toBe(SHORT);
  });

  it("says nothing when there is no error and nothing was missing", () => {
    expect(scheduleMissSentence({ last_error: null, last_missing: [] })).toBeNull();
    expect(scheduleMissSentence({ last_error: "", last_missing: undefined })).toBeNull();
    expect(scheduleMissSentence({ last_error: " \n ", last_missing: [] })).toBeNull();
  });

  it("calls scheduleMissSentence from the schedule row that already titles the miss", () => {
    const src = readFileSync(join(__dirname, "../app/workflows/library.tsx"), "utf8");
    const at = src.indexOf("s.last_missing?.length");
    expect(at).toBeGreaterThan(-1);
    const liStart = src.lastIndexOf("<li", at);
    const liEnd = src.indexOf("</li>", at);
    const rowSrc = src.slice(liStart, liEnd);
    expect(rowSrc).toContain("scheduleMissSentence(");
    expect(rowSrc).toContain("s.last_error");
    expect(rowSrc).toContain("wf-sched-refused-");
    expect(rowSrc).toContain("<p>");
  });

  it("shows a long failure as a paragraph and keeps the title and the warning", async () => {
    await mount([
      schedule({
        id: "sch_long",
        last_error: `  ${LONG_ERROR}  `,
        last_missing: [{ key: "host", label: "A deploy host", fix: "Add a host" }],
      }),
    ]);
    const li = row("sch_long");
    const paragraph = [...li.querySelectorAll("p")].find((p) => p.textContent === LONG);
    expect(paragraph).toBeTruthy();
    expect(paragraph!.tagName).toBe("P");
    expect(titled(li).getAttribute("title")).toBe(
      "Not started last time. Missing: A deploy host (Add a host)",
    );
    expect(li.querySelector("[data-testid='wf-sched-refused-sch_long']")).not.toBeNull();
    expect(li.querySelector("[aria-label='Did not start']")).not.toBeNull();
    expect(li.textContent).toContain("Mondays 08:00");
  });

  it("shows a short miss as words on the row, still titled with what was missing", async () => {
    await mount([
      schedule({
        id: "sch_six",
        describes: "Fridays 16:00",
        last_error: SIX_ERROR,
      }),
      schedule({
        id: "sch_miss",
        describes: "Daily 09:00",
        last_error: "   ",
        last_missing: [{ key: "host", label: "A deploy host", fix: "Add a host" }],
      }),
      schedule({ id: "sch_ok", describes: "Sundays 12:00", last_error: null, last_missing: [] }),
    ]);

    const six = row("sch_six");
    expect(six.textContent).toContain(SIX);
    expect(titled(six).getAttribute("title")).toBe(`Last run failed: ${SIX_ERROR}`);
    expect(six.querySelector("[data-testid='wf-sched-refused-sch_six']")).not.toBeNull();

    const missed = row("sch_miss");
    expect(missed.textContent).toContain(SHORT);
    expect(missed.textContent).not.toContain("A deploy host");
    expect(titled(missed).getAttribute("title")).toBe(
      "Not started last time. Missing: A deploy host (Add a host)",
    );
    expect(missed.querySelector("[data-testid='wf-sched-refused-sch_miss']")).not.toBeNull();

    const fine = row("sch_ok");
    expect(fine.textContent ?? "").not.toMatch(/Last run failed|Not started last time/);
    expect(fine.querySelector("[data-testid='wf-sched-refused-sch_ok']")).toBeNull();
    expect(titled(fine).getAttribute("title") ?? "").not.toMatch(/Last run failed|Not started/);
  });
});
