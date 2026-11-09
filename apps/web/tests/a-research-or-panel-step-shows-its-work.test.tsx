// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PALETTE } from "./_helpers/canvas-fixtures";
import { standingOf } from "./_helpers/run-standing";
import { codeOnly } from "./_helpers/source";

/**
 * A workflow run's research step shows ITS dossier, and its panel step shows ITS meeting, in
 * the step panel — not a link to another area. Research and Panels are off the nav (operator,
 * 2026-10-04): the run is the way in. The compact view leads with the verdict or the outcome,
 * shows live progress while the step runs (phases ticking, rounds filling in with seat faces),
 * and has "Open full view ↗" for the full page.
 *
 * Mounted for real (RunsView) with only `fetch` stood in, answering as the API does.
 */
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { RunsView, stepWorkOf } = await import("../app/delivery-workflows-ui");
const { forgetRunDefinitions } = await import("../app/workflows/flow/run-lane");

function step(id: string, capability: string, status: string, output: Record<string, unknown>) {
  return { step_id: id, kind: "capability", status, capability, output, evidence: [], error: null,
           approval: {}, due_at: null, timeout_applied: null, attempt: 1, started_at: null,
           completed_at: null, decision_id: null };
}

const RUN = {
  id: "wfr_1", status: "running", standing: standingOf("running"), workflow_key: "standard_product_delivery",
  workflow_name: "Standard Product Delivery", version: 1, step_count: 3, steps_done: 1,
  current_step: "meeting", waiting_on: null, blocked_reason: null, error: null, updated_at: null,
  actions: [], input: { title: "Lead triage inbox" },
  steps: [
    step("research", "research", "succeeded", { discovery_id: "drun_lead", capability: "research" }),
    step("meeting", "meeting", "running", { await: { kind: "meeting", id: "mrun_lead" } }),
    step("build", "build", "pending", {}),
  ],
};

const DOSSIER = {
  id: "drun_lead", project_id: "prj_1", status: "completed", phase: "done", title: "Lead triage inbox",
  seed: "Build a lead triage inbox.", context: null, usage: { cost_usd_micros: 1_030_000 },
  state: {
    gate: { verdict: "promising", confidence: 0.45, thesis_confidence: 0.25, cited_sources: 10,
            checks: { has_outcome: true, confidence_strong: false } },
    candidate_outcomes: [{ outcome: "A narrow batch-triage prototype", confidence_0_1: 0.45 }],
    signal_findings: [{ angle: "demand", finding: "40% cite unqualified leads", citation_url: "https://whatconverts.com/x" }],
    critique: { overall_confidence_0_1: 0.25, biggest_risk: "A thin, easily-copied layer" },
  },
};

const MEETING = {
  id: "mrun_lead", title: "PRD meeting: Lead triage inbox", status: "running", phase: "round2",
  participants: ["Product Manager", "UX Designer", "Facilitator / Chief of Staff"],
  created_at: "2026-10-04T10:00:00Z", completed_at: null, directive: "Build a lead triage inbox.",
  round1: { "Product Manager": "## PM view\n\nScore every enquiry against the ICP.",
            "UX Designer": "## UX view\n\nThe score must read in a second." },
  round2: { "Product Manager": "## Agree\n\nShip the narrow slice." },
  round1_gists: { "Product Manager": { headline: "Score against the ICP", points: ["one"] } },
  round2_gists: {},
};

let root: Root | null = null;
let host: HTMLElement | null = null;
const realClientWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");
let calls: string[] = [];

beforeEach(() => {
  forgetRunDefinitions();
  calls = [];
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get() { return (this as HTMLElement).classList?.contains("wf-lane-viewport") ? 4000 : 0; },
  });
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const url = String(input);
    calls.push(url);
    if (url.startsWith("/v1/discoveries/drun_lead")) return Response.json(DOSSIER);
    if (url.startsWith("/v1/meeting-rooms/mrun_lead")) return Response.json(MEETING);
    return Response.json({ error: "not in this test" }, { status: 503 });
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
  if (realClientWidth) Object.defineProperty(HTMLElement.prototype, "clientWidth", realClientWidth);
  else delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth;
});

async function settle(rounds = 10) {
  for (let i = 0; i < rounds; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

const noop = () => {};
async function mountRun() {
  act(() => root!.render(
    <RunsView runs={[RUN] as never} openRun={RUN.id} setOpenRun={noop} onAdvance={noop} busy={null}
              capabilities={PALETTE.capabilities as never}
              onRetryStep={noop} onSkipStep={noop} onRerun={noop} onStop={noop} onDelete={noop}
              onStopAndRerun={noop} onBuildOnThis={noop} onRecheck={noop} onSetTarget={noop}
              onPrepareTarget={noop} onRunPreview={noop} onBound={noop} projectId="prj_1"
              actor="ceo" roles={[]} targets={[]} hosts={[]} />));
  await settle();
}

const q = (sel: string) => document.querySelector<HTMLElement>(sel);

describe("which step has work to show", () => {
  it("a research step its dossier, a panel step its meeting — finished or still running", () => {
    expect(stepWorkOf(RUN.steps[0] as never)).toEqual({ kind: "discovery", id: "drun_lead" });
    expect(stepWorkOf(RUN.steps[1] as never)).toEqual({ kind: "meeting", id: "mrun_lead" });
    expect(stepWorkOf(step("m", "meeting", "succeeded", { meeting_run_id: "mrun_2" }) as never))
      .toEqual({ kind: "meeting", id: "mrun_2" });
    expect(stepWorkOf(step("r", "research", "running", { await: { kind: "discovery", id: "drun_2" } }) as never))
      .toEqual({ kind: "discovery", id: "drun_2" });
    expect(stepWorkOf(RUN.steps[2] as never)).toBeNull();
    expect(stepWorkOf(step("r", "research", "pending", {}) as never)).toBeNull();
  });
});

describe("the step panel", () => {
  it("opens on the running panel step: its rounds filling in with seat faces, live", async () => {
    await mountRun();
    const panel = q('[data-testid="dw-step-meeting"] [data-testid="meeting-compact"]');
    expect(panel).not.toBeNull();
    expect(calls.some((u) => u.startsWith("/v1/meeting-rooms/mrun_lead?project_id=prj_1"))).toBe(true);
    expect(q('[data-testid="meeting-compact-outcome"]')!.textContent).toBe("In session");
    expect(panel!.textContent).toContain("The seats are debating");
    const rounds = [...panel!.querySelectorAll(".rp-round-live")].map((r) => r.textContent);
    expect(rounds).toEqual(["Opening views2/2", "Debate1/2"]);
    expect(panel!.querySelectorAll('[data-filled="true"] .ag-avatar').length).toBe(3);
    const stages = [...panel!.querySelectorAll(".rp-ph")].map((p) => `${p.textContent}:${p.getAttribute("data-state")}`);
    expect(stages).toEqual(["✓Opening views:done", "→Debate:now", "3Synthesis:todo", "4PRD:todo", "5Build:todo"]);
    const full = panel!.querySelector<HTMLAnchorElement>('[data-testid="meeting-compact-open"]')!;
    expect(full.textContent).toBe("Open full view ↗");
    expect(new URLSearchParams(full.getAttribute("href")!.slice(1)).get("meeting")).toBe("mrun_lead");
    // The panel is on the step; the old "open the panel" link is not repeated beside it.
    expect(q('[data-testid="dw-step-links"]')?.textContent ?? "").not.toContain("open the panel");
  });

  it("shows the research step's verdict first, with the full dossier one press away", async () => {
    await mountRun();
    const node = q('.wf-node[data-step-id="research"]') ?? q('[data-step-id="research"]');
    expect(node).not.toBeNull();
    await act(async () => { node!.click(); });
    await settle();
    const dossier = q('[data-testid="dw-step-dossier"] [data-testid="dossier-compact"]');
    expect(dossier).not.toBeNull();
    expect(q('[data-testid="dossier-compact-verdict"]')!.textContent).toBe("Promising");
    expect(dossier!.textContent).toContain("Lead triage inbox");
    expect(dossier!.textContent).toContain("Best bet: A narrow batch-triage prototype");
    expect(dossier!.textContent).toContain("10 sources, 1 outcome, critic 25%");
    expect([...dossier!.querySelectorAll(".rp-chk")].map((c) => c.textContent))
      .toEqual(["✓ Has an outcome", "✕ Strong confidence"]);
    const full = dossier!.querySelector<HTMLAnchorElement>('[data-testid="dossier-compact-open"]')!;
    const params = new URLSearchParams(full.getAttribute("href")!.slice(1));
    expect([params.get("area"), params.get("run")]).toEqual(["discovery", "drun_lead"]);
    expect(q('[data-testid="dw-step-links"]')?.textContent ?? "").not.toContain("open the research");
  });
});

describe("the step panel places it once", () => {
  it("between the step's own state and its Output", () => {
    const DW = codeOnly("app/delivery-workflows-ui.tsx");
    const detail = DW.slice(DW.indexOf("function StepDetail"));
    expect(detail.indexOf("<DossierCompact")).toBeGreaterThan(detail.indexOf("<StepDoneBy"));
    expect(detail.indexOf("<DossierCompact")).toBeLessThan(detail.indexOf('<StepSection title="Output"'));
    expect(detail.match(/<MeetingCompact /g)).toHaveLength(1);
  });
});
