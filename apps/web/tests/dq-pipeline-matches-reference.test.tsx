// @vitest-environment happy-dom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { buildPipeline, designsFrom, kindKey } from "../lib/pipeline-model";
import { PipelineView } from "../app/workflows/pipeline-view";
import { AgentContractView } from "../app/workflows/editor/agent-contract-view";
import { wrapToWidth } from "../app/workflows/editor/agent-steps-map";
import type { Spec } from "../lib/workflow-editor-types";
import fixture from "./fixtures/catalogue-specs.json";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

/**
 * The shipped DQ RCA and Remediation pipeline (as the engine publishes it) drawn the way the
 * reference design draws it: every stage the flow runs numbered in run order,
 * conditions that say what they route on, the lanes named by what they fan out over, handoffs
 * as sentences with stage names, and the agent designs with the run trace and the Context
 * Graph's own edges. The rules are general; the DQ pipeline is the probe.
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

// The engine's published spec (scripts/dump-catalogue-specs.py; a test refuses a stale one).
const spec = (fixture as unknown as { catalogue: { workflow_key: string; spec: Spec }[] }).catalogue
  .find((f) => f.workflow_key === "dq_incident_rca")!.spec;
const stages = buildPipeline(spec, null);
const at = (id: string) => stages.find((s) => s.id === id)!;
const RAW_ID = /\b[a-z]+_[a-z_]+\b/;

describe("the DQ pipeline overview", () => {
  it("P0 numbers every stage the workflow runs, in run order, as the reference does", () => {
    expect(stages.filter((s) => s.number !== null).sort((a, b) => a.number! - b.number!)
      .map((s) => [s.number, s.id])).toEqual([
      [1, "monitoring"], [2, "context_enrichment"], [3, "historical_pattern"], [4, "rca"],
      [5, "pipeline_orchestrator"], [6, "lane"], [7, "remediation_advisor"], [8, "remediation_fix"],
      [9, "escalation"], [10, "incident_closure"],
    ]);
    // People are human steps; Rule Proposal sits in Rule authoring, reached from the cause checks.
    expect(at("human_approval").number).toBeNull();
    expect(at("human_execution").number).toBeNull();
    expect(at("rule_drafting").placement).toBe("side");
    expect(at("rule_drafting").sideLink).toBe("From Cause Checks: Confirmed false positive: rule proposal");
    expect(at("escalation").placement).toBe("side");
    // Nothing the flow runs is left as an unnumbered side path.
    for (const id of ["pipeline_orchestrator", "remediation_advisor", "remediation_fix", "incident_closure"]) {
      expect(at(id).placement, id).toBe("main");
    }
  });

  it("P0 draws a condition as what it routes on, never an empty box; the lanes say what they fan out over", () => {
    expect(at("incident_route").members).toBeUndefined();
    expect(at("incident_route").frameNote).toBe(
      "Yes: Evidence loop · No: Attached to an open incident, or not an Error — stop");
    expect(at("attached_stop").outcome).toBe(true);
    expect(at("lanes").frameNote).toBe(
      "In parallel, one per root-cause family, from Cause Router · up to 3 at once");
    expect(at("evidence_loop").frameNote).toContain("while it reports InsufficientEvidence");
    const el = mount(<PipelineView name="DQ RCA and Remediation" stages={stages} />);
    expect(el.textContent).not.toContain("No stage inside yet");
    expect(el.querySelector('[data-testid="route-incident_route"]')?.textContent).toContain("A new incident?");
    expect(el.querySelector('[data-testid="stage-attached_stop"]')).toBeNull();
    expect(el.querySelector('[data-testid="side-link-rule_drafting"]')).not.toBeNull();
    expect(el.textContent).not.toMatch(/unknown/i);
  });

  it("P0 hands off in full sentences that name stages by their display names", () => {
    expect(at("context_enrichment").contract!.handoff).toMatch(/^Returns the decision ID to the Cause Router, which invokes Past Incidents\./);
    expect(at("monitoring").contract!.handoff).toMatch(/^Returns the incident ID/);
    for (const s of stages) {
      const handoff = s.contract?.handoff ?? "";
      if (!handoff) continue;
      expect(handoff, s.id).toMatch(/^[A-Z].*\.$/);
      expect(handoff, s.id).not.toMatch(RAW_ID);
    }
    for (const id of ["monitoring", "context_enrichment", "historical_pattern"]) {
      expect(at(id).from, id).toBe("Cause Router");
    }
    expect(at("monitoring").to).toBe("Context Gathering");
    expect(at("context_enrichment").to).toBe("Past Incidents");
    expect(at("historical_pattern").to).toBe("Root Cause Analysis");
    // A policy value reads as words, never as its raw key.
    expect(JSON.stringify(at("context_enrichment").contract!.policy)).not.toContain("standard_profile");
  });

  it("P0 a generic handoff with no words of its own is composed from display names", () => {
    const generic = buildPipeline({
      steps: [
        { id: "first_step", kind: "task", title: "First", next: ["second_step"],
          contract: { reads: [], writes: [], handoff: { to: "second_step", carries: ["incident_id", "rca_verdict"] } } },
        { id: "second_step", kind: "task", title: "Second", next: [] },
      ],
    }, null);
    expect(generic[0].contract!.handoff).toBe("Hands the incident ID and RCA verdict to Second.");
  });
});

describe("the DQ agent designs", () => {
  const contextKinds = new Set(stages.flatMap((s) => (s.contract?.writes ?? []).map((w) => kindKey(w.kind))));
  const graph = { kgVersion: 3, cgVersion: 3, kinds: {}, contextKinds };

  it("P0 Incident Intake: the run trace, CORRELATED_WITH, the example, decisions once", () => {
    const el = mount(<AgentContractView stage={at("monitoring")} hasRun={false} graph={graph} />);
    expect(el.querySelector('[data-testid="map-node-agentrun"]')).not.toBeNull();
    expect(el.querySelector('[data-testid="map-node-agenttoolcall"]')).not.toBeNull();
    expect(el.querySelector('[data-testid="cg-link-incident-correlatedwith-incident"]')?.textContent).toBe("CORRELATED_WITH");
    expect(el.querySelector('[data-testid="design-example"]')?.textContent)
      .toBe("Example: DQX-0588 v2 on COB 2026-10-05 (locked running example).");
    const decisions = el.querySelector('[data-testid="card-decisions"]')!;
    expect(decisions.querySelectorAll("ul").length).toBe(0);
    expect(decisions.textContent!.match(/severity never goes down/g)).toHaveLength(1);
    expect(el.querySelector('[data-testid="steps-from"]')?.textContent).toContain("Cause Router");
    expect(el.querySelector('[data-testid="band-kg"]')?.textContent).toBe("Knowledge Graph v3 · read only");
  });

  it("P0 Context Gathering: the Service Desk is drawn, the policy has no raw value, labels never collide", () => {
    const el = mount(<AgentContractView stage={at("context_enrichment")} hasRun={false} graph={graph} />);
    expect(el.querySelector('[data-testid="map-system-servicedesk"]')).not.toBeNull();
    expect(el.textContent).not.toContain("standard_profile");
    expect(el.querySelector('[data-testid="steps-to"]')?.textContent).toContain("Past");
    // Each step name fits the room between its circle and the next.
    const names = [...el.querySelectorAll('[data-testid^="step-name-"]')].map((t) => t.textContent ?? "");
    const longest = Math.max(...names.map((n) => n.length));
    expect(longest).toBeLessThanOrEqual(15);
    expect(names).toContain("Upstream");
    expect(names).toContain("lineage");
  });

  it("P0 Historical Pattern: MATCHED_INCIDENT and REFERENCES are drawn between their kinds", () => {
    const el = mount(<AgentContractView stage={at("historical_pattern")} hasRun={false} graph={graph} />);
    expect(el.querySelector('[data-testid="cg-link-precedent-matchedincident-incident"]')?.textContent).toBe("MATCHED_INCIDENT");
    expect(el.querySelector('[data-testid="cg-link-precedent-references-resolvedpattern"]')?.textContent).toBe("REFERENCES");
    expect(el.querySelector('[data-testid="map-node-agentrun"]')).not.toBeNull();
  });

  it("P0 one Knowledge Graph version: the designs' reference when the pack declares it", () => {
    expect(designsFrom({ graph: { kg_version: 1, kg_reference: 17, cg_version: 4 } }).kgVersion).toBe(17);
    expect(designsFrom({ graph: { kg_version: 3, cg_version: 4 } }).kgVersion).toBe(3);
  });

  it("P1 wraps a step name word by word to the room it has", () => {
    expect(wrapToWidth("Upstream lineage", 14)).toEqual(["Upstream", "lineage"]);
    expect(wrapToWidth("Read the group", 20)).toEqual(["Read the group"]);
    expect(wrapToWidth("find_past_incidents", 12)).toEqual(["find_past_", "incidents"]);
  });
});
