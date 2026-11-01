// @vitest-environment happy-dom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { buildPipeline, kindLabel } from "../lib/pipeline-model";
import { PipelineView } from "../app/workflows/pipeline-view";
import { OUTSIDE, REG_PALETTE, REG_RUN, REG_SPEC } from "./_helpers/regulatory-pipeline-fixture";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

/**
 * The Pipeline tab as the consultant's screen (design §4.7): numbered stages, who runs each,
 * the loop and the lanes drawn as frames, a status from the last run (never "unknown"), and
 * the stage table — on the real NorthPeak regulatory pipeline's shape.
 */

let root: Root | null = null;
let host: HTMLElement | null = null;

function mount(el: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(el));
  return host;
}

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
});

const by = (id: string) => host!.querySelector(`[data-testid="${id}"]`) as HTMLElement | null;

describe("buildPipeline", () => {
  const stages = buildPipeline(REG_SPEC, REG_PALETTE, [OUTSIDE], REG_RUN);
  const at = (id: string) => stages.find((s) => s.id === id)!;

  it("P0 numbers the ten working stages in flow order; the loop and the lanes only frame them", () => {
    expect(stages.filter((s) => s.number !== null).map((s) => [s.number, s.title])).toEqual([
      [1, "Clause intake"], [2, "Context enrichment"], [3, "Historical pattern"],
      [4, "Compliance assessment"], [5, "Compliance orchestration"], [6, "Lane confirmation"],
      [7, "Remediation package"], [8, "Bank's own review"], [9, "Human approval"], [10, "Compliance closure"],
    ]);
    expect(at("evidence_loop").number).toBeNull();
    expect(at("evidence_loop").members).toEqual(["context_enrichment"]);
    expect(at("context_enrichment").group).toBe("evidence_loop");
    expect(at("lanes").members).toEqual(["lane_confirmation"]);
    expect(at("evidence_loop").frameNote).toBe("Repeats Context enrichment up to 2× while it reports missing kinds");
    expect(at("lanes").frameNote).toBe("One lane per finding family, from Compliance orchestration · up to 3 at once");
  });

  it("P0 says what runs each stage, in words", () => {
    expect(at("context_enrichment").runsAs.label).toBe("Deterministic · no model");
    expect(at("compliance_assessment").runsAs.label).toBe("Tally · Compliance Analyst");
    expect(at("lane_confirmation").runsAs.label).toBe("Plumb · Lane Agent");
    expect(at("remediation_package").runsAs.label).toBe("Mend · Remediation Advisor");
    expect(at("compliance_orchestration").runsAs.label).toBe("Pipeline Orchestrator");
    // One wording for every no-model step, built-in or graph query.
    expect(at("clause_intake").runsAs.label).toBe("Deterministic · no model");
    expect(at("clause_intake").runsAs.tone).toBe("deterministic");
    expect(at("human_approval").runsAs.label).toBe("Person · Compliance Officer");
    expect(at("bank_review").runsAs.label).toBe("Outside agent · NorthPeak review agent");
    expect(at("evidence_loop").runsAs.label).toBe("Bounded loop (up to 2)");
    expect(at("lanes").runsAs.label).toBe("For each lane");
  });

  it("P0 takes each status from the last run, and says 'Not run yet' before any run", () => {
    expect(at("compliance_assessment").status).toBe("succeeded");
    expect(at("human_approval").status).toBe("waiting");
    expect(at("compliance_closure").status).toBe("pending");
    const before = buildPipeline(REG_SPEC, REG_PALETTE, [OUTSIDE], null);
    expect(new Set(before.map((s) => s.status))).toEqual(new Set(["not_run"]));
  });

  it("P1 describes each stage in one plain line from its step, capability or graph step", () => {
    expect(at("context_enrichment").description).toMatch(/^Collects the rules, code/);
    expect(at("human_approval").description).toMatch(/^Approve the remediation package/);
    expect(at("bank_review").description).toBe("Reads clause, agent decision, remediation package; writes agent decision.");
    expect(at("compliance_assessment").description).toMatch(/^Classify/);
  });

  it("P1 spells graph kinds as words however the contract spells them", () => {
    expect(kindLabel("agent_decision")).toBe("Agent decision");
    expect(kindLabel("RegulatoryReport")).toBe("Regulatory report");
    expect(kindLabel("CDE")).toBe("CDE");
  });
});

describe("PipelineView", () => {
  const stages = buildPipeline(REG_SPEC, REG_PALETTE, [OUTSIDE], REG_RUN);

  it("P0 draws the numbered stages, the loop frame with its loop-back, and the lane frame", () => {
    mount(<PipelineView name="NorthPeak regulatory compliance" stages={stages} run={REG_RUN} />);
    expect(by("stage-clause_intake")!.textContent).toContain("Clause intake");
    expect(by("loop-evidence_loop")!.querySelector('[data-testid="stage-context_enrichment"]')).not.toBeNull();
    expect(by("loop-back-evidence_loop")).not.toBeNull();
    expect(by("lane-lanes")!.querySelector('[data-testid="stage-lane_confirmation"]')).not.toBeNull();
    expect(by("stage-runs-as-compliance_assessment")!.textContent).toBe("Tally · Compliance Analyst");
    expect(by("stage-human_approval")!.textContent).toContain("Waiting");
    expect(by("pipeline-last-run")!.textContent).toContain("Waiting for a person");
    expect(host!.textContent).not.toMatch(/unknown/i);
  });

  it("P0 lists every stage in the table with what it does, who runs it and its status", () => {
    mount(<PipelineView name="NorthPeak" stages={stages} run={REG_RUN} />);
    const row = by("stage-row-lane_confirmation")!;
    expect(row.textContent).toContain("Lane confirmation");
    expect(row.textContent).toContain("Confirm or rule out");
    expect(row.textContent).toContain("Plumb · Lane Agent");
    expect(row.textContent).toContain("Succeeded");
    expect(by("stage-row-compliance_closure")!.textContent).toContain("Pending");
  });

  it("P0 says 'Not run yet' on every stage of a workflow that has never run", () => {
    const fresh = buildPipeline(REG_SPEC, REG_PALETTE, [OUTSIDE], null);
    mount(<PipelineView name="NorthPeak" stages={fresh} run={null} />);
    const pills = [...host!.querySelectorAll('[data-testid="stage-status"]')].map((p) => p.textContent);
    expect(new Set(pills)).toEqual(new Set(["Not run yet"]));
  });

  it("P0 clicking a stage opens its read/write view; clicking again closes it", () => {
    mount(<PipelineView name="NorthPeak" stages={stages} run={REG_RUN} />);
    expect(by("pipeline-stage-detail")).toBeNull();
    act(() => by("stage-context_enrichment")!.click());
    const detail = by("pipeline-stage-detail")!;
    expect(detail.querySelector('[data-area="agent-contract-view"]')).not.toBeNull();
    expect(detail.textContent).toContain("Knowledge Graph · read only");
    act(() => by("stage-context_enrichment")!.click());
    expect(by("pipeline-stage-detail")).toBeNull();
  });

  it("P0 shows no raw ids anywhere: no step keys, node ids, run ids or outside-agent ids", () => {
    mount(<PipelineView name="NorthPeak" stages={stages} run={REG_RUN} outsideAgents={[OUTSIDE]} onAssignCaptain={() => {}} />);
    act(() => by("stage-bank_review")!.click());
    const text = host!.textContent ?? "";
    expect(text).not.toMatch(/\b[a-z]+_[a-z_]+\b/); // compliance_assessment, agent_decision …
    expect(text).not.toMatch(/\b(kgn|wfr|ext)_[0-9a-f]+/);
  });

  it("P1 keeps the outside-agent captain switcher (KG P6b)", () => {
    const assign = vi.fn();
    mount(<PipelineView name="NorthPeak" stages={stages} run={REG_RUN} outsideAgents={[OUTSIDE]} onAssignCaptain={assign} />);
    expect(by("stage-captain-select-remediation_package")).toBeNull();
    act(() => by("stage-remediation_package")!.click());
    const select = by("stage-captain-select-remediation_package") as HTMLSelectElement;
    expect([...select.options].map((o) => o.textContent)).toContain("NorthPeak review agent (outside)");
    act(() => {
      select.value = `outside:${OUTSIDE.id}`;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(assign).toHaveBeenCalledWith("remediation_package", { kind: "outside_agent", value: OUTSIDE.id });
    // A person's gate is never offered to an agent.
    act(() => by("stage-human_approval")!.click());
    expect(by("stage-captain-select-human_approval")).toBeNull();
    expect(by("outside-agent-host")!.textContent).toBe("review.northpeak.example");
  });

  it("P1 an outside agent's card is as compact as the others: one chip, its host, 'reads N · writes N'", () => {
    mount(<PipelineView name="NorthPeak" stages={stages} run={REG_RUN} outsideAgents={[OUTSIDE]} />);
    const card = by("stage-bank_review")!;
    expect(card.querySelector('[data-testid="outside-agent-badge"]')).toBeNull();
    expect(card.querySelectorAll(".pl-runs-as")).toHaveLength(1);
    expect(card.querySelector('[data-testid="outside-agent-host"]')!.textContent).toBe("review.northpeak.example");
    expect(by("stage-contract-bank_review")!.textContent).toBe("reads 3 · writes 1");
    expect(card.textContent).not.toContain("reads Clause");
  });

  it("P1 an empty workflow says so", () => {
    mount(<PipelineView name="Draft" stages={[]} run={null} />);
    expect(by("pipeline-empty")).not.toBeNull();
  });
});
