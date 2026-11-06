// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../app/workflows/studio-parts", () => import("./_helpers/studio-parts-stub"));

/**
 * "Open report": the run report was a collapsed panel below the lane — "buried and not exposed".
 * Its bar now has Open report, which opens a dialog with the WHOLE report, every section open;
 * and a finished run offers the same press at the top of its detail, "so when people look into
 * the finished run they can view what was done". A run still going has no top button.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { DeliveryWorkflows } = await import("../app/delivery-workflows-ui");
const { RunReportPanel } = await import("../app/run-report-ui");

const step = (id: string, status: string) => ({
  step_id: id, kind: "capability", status, capability: id, output: {}, evidence: [], error: null,
  approval: {}, due_at: null, timeout_applied: null, attempt: 1, started_at: null, completed_at: null,
});
const runOf = (finished: boolean) => ({
  id: "wfr_1", status: finished ? "completed" : "running", standing: finished ? "closed" : "active",
  workflow_key: "minor_feature", workflow_name: "Minor Feature", version: 1, step_count: 2,
  steps_done: finished ? 2 : 1, current_step: finished ? null : "build", waiting_on: null,
  blocked_reason: null, error: null, updated_at: null, input: { title: "Lead triage" }, finished,
  actions: [],
  steps: [step("research", "succeeded"), step("build", finished ? "succeeded" : "running")],
});
const REPORT = {
  ok: true, workflow: "Minor Feature", status: "completed", elapsed_minutes: 90, working_minutes: 81.6,
  waiting_minutes: 361, total_usd: 14, list_usd: 14,
  steps: [{ step_id: "research", label: "Research", status: "succeeded", attempt: 1, usd: 1, seconds: 60 }],
  contributors: [{ role: "engineer", calls: 3, turns: 9, usd: 12, models: [] }],
  findings: [],
  product: { name: "Lead triage", prd: { summary: "Sort inbound leads." },
             screens: [{ name: "Inbox", does: "lists leads" }],
             acceptance: [{ id: "AC1", text: "a lead can be assigned" }],
             build: { files_changed: 4, verdicts: { review: "clean" } } },
  next_steps: [{ priority: "P2", area: "tests", item: "cover assignment", why: "untested" }],
};
const VIEWS: Record<string, unknown> = {
  capabilities: { capabilities: [], blocks: [], manual_approval_risk_floor: 2 },
  catalogue: { items: [] }, installed: { items: [] },
  custom_steps: { steps: [], bindable: { skills: [], mcp_servers: [] } },
  schedules: { schedules: [] },
};

let run = runOf(true);
let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const url = new URL(String(input), "http://console.test");
    if (url.pathname.includes("workflow-report")) return Response.json(REPORT);
    const view = url.searchParams.get("view") ?? "";
    if (view === "runs") return Response.json({ runs: [run] });
    return Response.json(VIEWS[view] ?? {});
  }));
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 20; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
const dialog = () => document.querySelector<HTMLElement>('[data-testid="run-report-dialog"]');

describe("Open report", () => {
  it("sits on the report's bar and opens the whole report, every section open, without toggling the bar", async () => {
    await act(async () => root!.render(<RunReportPanel runId="wfr_1" projectId="p1" />));
    await settle();
    const panel = host!.querySelector<HTMLDetailsElement>('[data-testid="run-report"]')!;
    const open = panel.querySelector<HTMLButtonElement>('summary [data-testid="run-report-open"]');
    expect(open, "Open report is in the bar").not.toBeNull();
    expect(open!.textContent).toContain("Open report");
    await act(async () => { open!.click(); });
    await settle();
    expect(panel.open, "the press does not unfold the panel").toBe(false);
    const d = dialog();
    expect(d, "the dialog opened").not.toBeNull();
    expect(d!.getAttribute("aria-modal")).toBe("true");
    expect(host!.contains(d), "portalled to the body").toBe(false);
    const sections = Array.from(d!.querySelectorAll<HTMLDetailsElement>("details"));
    expect(sections.length).toBeGreaterThanOrEqual(5);
    expect(sections.every((s) => s.open), "every section is open").toBe(true);
    expect(d!.textContent).toContain("a lead can be assigned");
    expect(d!.querySelector('[data-testid="run-report-save"]'), "the PDF stays inside").not.toBeNull();
  });

  it("closes on Esc and on its close button", async () => {
    await act(async () => root!.render(<RunReportPanel runId="wfr_1" projectId="p1" />));
    await settle();
    const open = host!.querySelector<HTMLButtonElement>('[data-testid="run-report-open"]')!;
    await act(async () => { open.click(); });
    await settle();
    expect(dialog()).not.toBeNull();
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); });
    expect(dialog()).toBeNull();
    await act(async () => { open.click(); });
    await settle();
    await act(async () => {
      document.querySelector<HTMLButtonElement>('[data-testid="run-report-dialog-close"]')!.click();
    });
    expect(dialog()).toBeNull();
  });

  it("leads a finished run's bar, and opens the same dialog", async () => {
    run = runOf(true);
    await act(async () => root!.render(<DeliveryWorkflows projectId="p1" actor="ceo" />));
    await settle();
    const top = host!.querySelector<HTMLButtonElement>('.wf-run-bar [data-testid="run-open-report"]');
    expect(top, "a finished run offers Open report at the top").not.toBeNull();
    await act(async () => { top!.click(); });
    await settle();
    const d = dialog();
    expect(d).not.toBeNull();
    expect(Array.from(d!.querySelectorAll<HTMLDetailsElement>("details")).every((s) => s.open)).toBe(true);
  });

  it("is not at the top of a run still going", async () => {
    run = runOf(false);
    await act(async () => root!.render(<DeliveryWorkflows projectId="p1" actor="ceo" />));
    await settle();
    expect(host!.querySelector('[data-testid="dw-path"]'), "the run detail is drawn").not.toBeNull();
    expect(host!.querySelector('[data-testid="run-open-report"]')).toBeNull();
  });
});
