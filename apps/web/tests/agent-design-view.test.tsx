// @vitest-environment happy-dom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { buildPipeline, designsFrom } from "../lib/pipeline-model";
import { PipelineView } from "../app/workflows/pipeline-view";
import { AgentContractView } from "../app/workflows/editor/agent-contract-view";
import { DQ_DESIGNS, DQ_PALETTE, DQ_RUN, DQ_SPEC } from "./_helpers/dq-design-fixture";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

/**
 * The consultant's agent design in the Pipeline tab (design §4.6, §4.7): design status and
 * reference beside the last run, shared services and side paths off the flow, the agent's own
 * numbered steps with what each reads and writes, the design cards, and the review buttons.
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
const click = (el: Element | null) => act(() => { el!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
const stages = (designs = DQ_DESIGNS, run = DQ_RUN) => buildPipeline(DQ_SPEC, DQ_PALETTE, [], run, designs);
const at = (id: string, list = stages()) => list.find((s) => s.id === id)!;

function view(stageId: string, opts: { run?: boolean; onDesignAction?: (a: string) => void; error?: string } = {}) {
  const list = stages(DQ_DESIGNS, opts.run === false ? (null as never) : DQ_RUN);
  const contextKinds = new Set(list.flatMap((s) => (s.contract?.writes ?? []).map((w) => w.kind.replace(/[^a-z0-9]/gi, "").toLowerCase())));
  mount(<AgentContractView stage={at(stageId, list)} hasRun={opts.run !== false}
                           graph={{ kgVersion: 17, cgVersion: 4, kinds: DQ_DESIGNS.kinds, contextKinds }}
                           onDesignAction={opts.onDesignAction} designError={opts.error} />);
}

describe("the design model", () => {
  it("P0 takes each stage's design from its review record, else its contract, else its kind", () => {
    const list = stages(designsFrom({ stages: {} }));
    expect(at("monitoring", list).design.status).toBe("locked");
    expect(at("monitoring", list).design.reference).toBe("Agent Reference v0.1");
    expect(at("monitoring", list).design.can.revise).toBe(true);
    expect(at("context_enrichment", list).design.status).toBe("in_review");
    expect(at("rca", list).design.status).toBe("not_reviewed");
    expect(at("human_approval", list).design.status).toBe("human_step");
    expect(at("human_approval", list).design.can).toEqual({ send_for_review: false, lock: false, revise: false });
    // The review record wins over what the contract declares.
    const reviewed = stages(designsFrom({ stages: { monitoring: { status: "in_review", reference: "Agent Reference v0.2",
      source: "review", locked: { reference: "Agent Reference v0.1" }, can: { lock: true } } } }));
    expect(at("monitoring", reviewed).design.status).toBe("in_review");
    expect(at("monitoring", reviewed).design.locked?.reference).toBe("Agent Reference v0.1");
  });

  it("P0 puts a shared service and a side path off the flow, unnumbered, and anchors each agent between its neighbours", () => {
    const list = stages();
    expect(list.filter((s) => s.number !== null).map((s) => [s.number, s.title])).toEqual([
      [1, "Monitoring Agent"], [2, "Context Enrichment Agent"], [3, "RCA Agent"], [4, "Human approval"]]);
    expect(at("context_curator", list).placement).toBe("shared");
    expect(at("rule_drafting", list).placement).toBe("side");
    expect(at("rule_drafting", list).sideLabel).toBe("Rule authoring");
    expect(at("monitoring", list).to).toBe("Context Enrichment Agent");
    expect(at("context_enrichment", list).from).toBe("Monitoring Agent");
    expect(at("monitoring", list).contract!.steps!.map((s) => s.n)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9]);
    expect(at("monitoring", list).contract!.design!.decisions[0]).toEqual({ id: "A-D8", text: "severity only moves up" });
  });
});

describe("the pipeline's design status column", () => {
  it("P0 shows Design and Reference beside the last run, each clearly labelled", () => {
    mount(<PipelineView name="RCA and remediation pipeline" stages={stages()} run={DQ_RUN} designs={DQ_DESIGNS} />);
    const head = by("pipeline-stage-table")!.querySelector("thead")!.textContent!;
    expect(head).toContain("Design");
    expect(head).toContain("Reference");
    expect(head).toContain("Last run");
    expect(by("row-design-monitoring")!.textContent).toBe("Locked");
    expect(by("row-design-monitoring")!.getAttribute("data-design")).toBe("locked");
    expect(by("row-reference-monitoring")!.textContent).toBe("Agent Reference v0.1 · under revision");
    expect(by("row-design-context_enrichment")!.textContent).toBe("In review");
    expect(by("row-design-rca")!.textContent).toBe("Not yet reviewed");
    expect(by("row-reference-rca")!.textContent).toBe("—");
    expect(by("row-design-human_approval")!.textContent).toBe("Human step");
    expect(by("row-design-context_curator")!.textContent).toBe("Shared service");
    expect(by("row-reference-context_curator")!.textContent).toBe("Chapter to write");
    expect(by("stage-row-monitoring")!.textContent).toContain("Succeeded");
    expect(by("stage-row-context_curator")!.querySelector(".pl-cell-num")!.textContent).toBe("S");
  });

  it("P0 draws the shared services row and the rule-authoring area off the main flow, with the legend", () => {
    mount(<PipelineView name="RCA" stages={stages()} run={DQ_RUN} designs={DQ_DESIGNS} />);
    expect(by("shared-services")!.textContent).toContain("Shared services");
    expect(by("shared-services")!.querySelector('[data-testid="stage-context_curator"]')).not.toBeNull();
    expect(by("side-paths")!.textContent).toContain("Rule authoring");
    expect(by("side-paths")!.querySelector('[data-testid="stage-rule_drafting"]')!.getAttribute("data-placement")).toBe("side");
    expect(by("stage-monitoring")!.getAttribute("data-design")).toBe("locked");
    expect(by("stage-context_enrichment")!.getAttribute("data-design")).toBe("in_review");
    const legend = by("design-legend")!.textContent!;
    for (const word of ["Locked", "In review", "Not yet reviewed", "Human step", "Shared service", "Loop or side path"]) {
      expect(legend).toContain(word);
    }
  });

  it("P0 shows no raw ids anywhere", () => {
    mount(<PipelineView name="RCA" stages={stages()} run={DQ_RUN} designs={DQ_DESIGNS} />);
    act(() => by("stage-monitoring")!.click());
    const text = host!.textContent!;
    expect(text).not.toMatch(/\b(kgn|wfr|wfd|wfv|wsd|ext)_[0-9a-z]{6,}/i);
    expect(text).not.toMatch(/\b(context_enrichment|data_quality_rule|rule_drafting|context_curator)\b/);
  });
});

describe("the agent's steps", () => {
  it("P0 draws the numbered steps between the stage before and the next one, under versioned bands", () => {
    view("monitoring");
    expect(by("agent-steps-map")).not.toBeNull();
    for (let n = 1; n <= 9; n += 1) expect(by(`step-${n}`)).not.toBeNull();
    expect(by("step-1")!.textContent).toContain("Read the group");
    expect(by("step-9")!.textContent).toContain("Write trace,");
    expect(by("steps-to")!.textContent).toContain("To Context");
    expect(by("band-kg")!.textContent).toBe("Knowledge Graph v17 · read only");
    expect(by("band-cg")!.textContent).toBe("Context Graph v4 · read and write");
    expect(by("band-mcp")!.textContent).toBe("External systems · via MCP");
    expect(by("band-steps")!.textContent).toBe("Monitoring Agent · steps");
  });

  it("P0 colours Knowledge Graph kinds by their domain and draws Context Graph kinds as hexagons", () => {
    view("monitoring");
    const rule = by("map-node-dataqualityrule")!;
    expect(by("steps-kg")!.contains(rule)).toBe(true);
    expect(rule.querySelector("rect")!.getAttribute("style")).toContain("#c0392b");
    expect(by("map-node-cde")!.querySelector("rect")!.getAttribute("style")).toContain("#17866b");
    expect(by("steps-cg")!.querySelector('[data-testid="map-node-incident"] polygon')).not.toBeNull();
    expect(by("steps-cg")!.querySelector('[data-testid="map-node-agentdecision"] polygon')).not.toBeNull();
    expect(by("map-system-eagle")!.textContent).toBe("Eagle");
    expect(by("map-system-itsm")!.textContent).toBe("ITSM");
  });

  it("P0 selecting step 7 lights only its lines: DataQualityRule, CDE, Incident, AgentDecision and ITSM", () => {
    view("monitoring");
    click(by("step-7"));
    const on = [...host!.querySelectorAll('[data-testid^="line-"][data-on="true"]')].map((l) => l.getAttribute("data-testid"));
    expect(on.sort()).toEqual([
      "line-call-7-itsm-createticket", "line-read-7-cde", "line-read-7-dataqualityrule",
      "line-write-7-agentdecision", "line-write-7-incident"]);
    expect(by("line-read-1-agent")!.getAttribute("data-on")).toBe("false");
    expect(by("map-node-dataqualityrule")!.getAttribute("data-on")).toBe("true");
    expect(by("map-node-system")!.getAttribute("data-on")).toBe("false");
    expect(by("step-detail")!.textContent).toContain("Step 7 · Open or attach");
    expect(by("step-detail")!.textContent).toContain("ITSM (create ticket)");
    click(by("step-7"));
    expect(by("step-detail")).toBeNull();
  });

  it("P0 selecting a node lights the steps that read it and says which", () => {
    view("monitoring");
    click(by("map-node-dataqualityrule"));
    expect(by("step-2")!.getAttribute("data-on")).toBe("true");
    expect(by("step-4")!.getAttribute("data-on")).toBe("true");
    expect(by("step-1")!.getAttribute("data-on")).toBe("false");
    expect(by("map-node-detail")!.textContent).toContain("Read by steps 2, 4, 5, 7");
    expect(by("step-1")!.textContent).toContain("1");
  });

  it("P1 'Show the last run' marks nodes and steps with what the run did", () => {
    view("monitoring");
    click(by("show-last-run"));
    expect(by("map-node-dataqualityrule")!.getAttribute("data-overlay")).toBe("done");
    expect(by("map-node-dataqualityrule")!.textContent).toContain("read 1");
    expect(by("map-node-system")!.getAttribute("data-overlay")).toBe("missing");
    expect(by("map-node-incident")!.textContent).toContain("wrote 1");
    expect(by("step-7")!.getAttribute("data-run")).toBe("done");
    expect(by("step-1")!.getAttribute("data-run")).toBe("missing");
    expect(by("step-3")!.getAttribute("data-run")).toBe("partly");
    expect(by("map-system-itsm")!.getAttribute("data-overlay")).toBe("done");
    expect(by("map-system-eagle")!.getAttribute("data-overlay")).toBe("missing");
    click(by("step-3"));
    expect(by("step-detail")!.textContent).toContain("1 of 2 done; not done: DQThreshold");
  });

  it("P1 a stage without steps keeps the single-box read/write view", () => {
    view("rca");
    expect(by("agent-steps-map")).toBeNull();
    expect(by("contract-map")).not.toBeNull();
    expect(host!.textContent).toContain("Knowledge Graph v17 · read only");
  });
});

describe("the design cards", () => {
  it("P0 lays out the consultant's cards with their IDs", () => {
    view("monitoring");
    const cards = by("contract-cards")!;
    const titles = [...cards.querySelectorAll(".pl-design-card-title")].map((t) => t.textContent);
    expect(titles).toEqual(["Under revision", "Trigger", "Handoff", "Human review", "Applied in KG v16 / CG v4",
                            "Policy", "Open questions", "Decisions"]);
    expect(by("decision-notes")!.textContent).toBe("A-D8 severity only moves up · A-D9 link facts only");
    expect(by("decision-notes")!.querySelector('[title="A-D8: severity only moves up"]')).not.toBeNull();
    expect(by("policy-notes")!.textContent).toContain("OP-SEV-02 severity ladder");
    expect(by("card-applied-in")!.textContent).toContain("linkType on CORRELATED_WITH (CG-G2)");
    expect(by("card-under-revision")!.textContent).toContain("Rule authoring changed DQ grading");
    expect(by("card-applied-in")!.getAttribute("data-tone")).toBe("graph");
  });
});

describe("design review and lock", () => {
  it("P0 offers exactly the transition a stage can take, and sends it", () => {
    const onDesignAction = vi.fn();
    view("context_enrichment", { onDesignAction });
    expect(by("design-status")!.textContent).toBe("In review");
    expect(by("design-reference")!.textContent).toBe("Agent Reference v0.2");
    expect(by("design-send-for-review")).toBeNull();
    expect(by("design-revise")).toBeNull();
    click(by("design-lock"));
    expect(onDesignAction).toHaveBeenCalledWith("lock");
    act(() => root?.unmount());
    host?.remove();
    view("monitoring", { onDesignAction });
    expect(by("design-lock")).toBeNull();
    click(by("design-revise"));
    expect(onDesignAction).toHaveBeenLastCalledWith("revise");
    act(() => root?.unmount());
    host?.remove();
    view("rca", { onDesignAction });
    click(by("design-send-for-review"));
    expect(onDesignAction).toHaveBeenLastCalledWith("review");
  });

  it("P0 a person's step and a shared service offer no review, and a viewer without rights sees no buttons", () => {
    view("human_approval", { onDesignAction: vi.fn() });
    expect(by("design-bar")!.querySelector("button")).toBeNull();
    act(() => root?.unmount());
    host?.remove();
    view("context_enrichment");
    expect(by("design-lock")).toBeNull();
  });

  it("P0 the pipeline runs the action for the selected stage and shows a refusal in words", async () => {
    const onDesignAction = vi.fn().mockRejectedValue(new Error("Only a person can lock an agent design; agents propose, people lock."));
    mount(<PipelineView name="RCA" stages={stages()} run={DQ_RUN} designs={DQ_DESIGNS} onDesignAction={onDesignAction} />);
    act(() => by("stage-context_enrichment")!.click());
    await act(async () => { by("design-lock")!.click(); });
    expect(onDesignAction).toHaveBeenCalledWith("context_enrichment", "lock");
    expect(by("design-error")!.textContent).toContain("Only a person can lock");
  });
});
