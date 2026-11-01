// @vitest-environment happy-dom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { AgentContractView } from "../app/workflows/editor/agent-contract-view";
import { buildPipeline } from "../lib/pipeline-model";
import type { RunContext } from "../lib/workflow-api";
import { OUTSIDE, REG_PALETTE, REG_RUN, REG_SPEC } from "./_helpers/regulatory-pipeline-fixture";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

/**
 * The agent read/write view (design §4.7.2): three bands from the stage's contract, lines you
 * can highlight, and "Show the last run" overlaying what the run's graph record says it did.
 */

let root: Root | null = null;
let host: HTMLElement | null = null;

function mount(stageId: string, opts: { run?: boolean; trace?: RunContext } = {}) {
  const run = opts.run === false ? null : (opts.trace ?? REG_RUN);
  const stage = buildPipeline(REG_SPEC, REG_PALETTE, [OUTSIDE], run).find((s) => s.id === stageId)!;
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(<AgentContractView stage={stage} hasRun={!!run} />));
}

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
});

const by = (id: string) => host!.querySelector(`[data-testid="${id}"]`) as HTMLElement | null;
const click = (el: Element | null) => act(() => { el!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });

describe("AgentContractView", () => {
  it("P0 puts the contract's reads in the Knowledge Graph band and its writes in the Context Graph band, required marked", () => {
    mount("context_enrichment");
    const kg = by("kg-band")!;
    expect(kg.querySelector('[data-testid="node-read-clause"]')).not.toBeNull();
    expect(kg.querySelector('[data-testid="node-read-regulatoryreport"]')!.textContent).toContain("via reported in");
    const cg = by("context-band")!;
    const decision = cg.querySelector('[data-testid="node-write-agentdecision"]')!;
    expect(decision.getAttribute("data-required")).toBe("true");
    expect(decision.textContent).toContain("ContextAssembled");
    expect(cg.querySelector('[data-testid="node-write-coveragegap"]')!.getAttribute("data-required")).toBeNull();
    expect(host!.textContent).toContain("Knowledge Graph (read only)");
    expect(host!.textContent).toContain("Context Graph (read & write)");
  });

  it("P0 draws reads, writes and MCP calls as their own kind of line", () => {
    mount("historical_pattern");
    const call = by("edge-call-itsm-get_incident")!;
    expect(call.getAttribute("class")).toContain("pl-edge-call");
    expect(by("node-call-itsm-get_incident")!.textContent).toContain("ITSM");
    expect(by("node-call-itsm-get_incident")!.textContent).toContain("get incident");
    mount("context_enrichment");
    expect(by("edge-read-rule")!.getAttribute("class")).toContain("pl-edge-read");
    expect(by("edge-write-agentdecision")!.getAttribute("class")).toContain("pl-edge-write");
  });

  it("P0 clicking a node highlights its line and dims the others", () => {
    mount("context_enrichment");
    click(by("node-read-rule"));
    expect(by("edge-read-rule")!.getAttribute("data-highlighted")).toBe("true");
    expect(by("edge-read-clause")!.getAttribute("data-dim")).toBe("true");
    expect(by("node-read-rule")!.getAttribute("aria-pressed")).toBe("true");
  });

  it("P0 'Show the last run' is off and explained before the workflow has run", () => {
    mount("context_enrichment", { run: false });
    const toggle = by("show-last-run") as HTMLButtonElement;
    expect(toggle.disabled).toBe(true);
    expect(toggle.getAttribute("title")).toBe("This workflow has not run yet");
  });

  it("P0 overlays the last run: done, declared but not done, and done but not declared", () => {
    mount("context_enrichment");
    click(by("show-last-run"));
    expect(by("show-last-run")!.getAttribute("aria-pressed")).toBe("true");
    expect(by("node-read-rule")!.getAttribute("data-overlay")).toBe("done");
    expect(by("node-read-rule")!.textContent).toContain("read 1");
    expect(by("node-read-clause")!.getAttribute("data-overlay")).toBe("missing");
    expect(by("node-read-clause")!.textContent).toContain("declared, not done");
    expect(by("node-read-incident")!.getAttribute("data-overlay")).toBe("undeclared");
    expect(by("node-read-incident")!.textContent).toContain("done, not declared");
    expect(by("node-write-agentdecision")!.getAttribute("data-overlay")).toBe("done");
    expect(by("node-write-coveragegap")!.getAttribute("data-overlay")).toBe("missing");
    click(by("node-read-symbol"));
    expect(by("node-detail")!.textContent).toContain("the last run read 11");
    expect(by("node-detail")!.textContent).toContain("LoanTransactionType.isRepaymentType");
  });

  it("P1 a stage the last run never reached cannot be overlaid, and says why", () => {
    mount("compliance_closure");
    const toggle = by("show-last-run") as HTMLButtonElement;
    expect(toggle.disabled).toBe(true);
    expect(toggle.getAttribute("title")).toBe("This stage was not reached on the last run");
  });

  it("P1 an outside agent's calls the run does not record say 'not recorded', never 'not done'", () => {
    mount("historical_pattern");
    click(by("show-last-run"));
    expect(by("node-call-itsm-get_incident")!.textContent).toContain("not recorded");
  });

  it("P1 a deterministic step that records its calls says 'called' and 'declared, not done' truthfully", () => {
    const trace: RunContext = { ...REG_RUN, steps: { ...REG_RUN.steps, historical_pattern: {
      ...REG_RUN.steps.historical_pattern,
      calls: [{ tool: "list_incidents", server: "itsm", count: 1 }], callsRecorded: true } } };
    mount("historical_pattern", { trace });
    click(by("show-last-run"));
    const listed = by("node-call-itsm-list_incidents")!;
    expect(listed.textContent).toContain("list incidents · called 1");
    expect(listed.getAttribute("data-made")).toBe("true");
    const got = by("node-call-itsm-get_incident")!;
    expect(got.textContent).toContain("get incident · declared, not done");
    expect(got.getAttribute("data-overlay")).toBe("missing");
    expect(got.textContent).not.toContain("not recorded");
  });

  it("P1 a stage with no contract says nothing is checked, and the run still shows what it touched", () => {
    mount("compliance_assessment");
    expect(by("no-contract")!.textContent).toContain("declares no contract yet");
    click(by("show-last-run"));
    expect(by("node-read-rule")!.getAttribute("data-overlay")).toBe("undeclared");
    expect(by("node-write-agentdecision")!.getAttribute("data-overlay")).toBe("undeclared");
  });

  it("P1 shows the design cards the contract carries", () => {
    mount("context_enrichment");
    const cards = by("contract-cards")!;
    expect(cards.textContent).toContain("Trigger");
    expect(cards.textContent).toContain("after intake opens a new case");
    expect(cards.textContent).toContain("Max hops");
    expect(cards.textContent).toContain("A-D32 no LLM for retrieval");
    expect(cards.textContent).toContain("None: no model is used");
  });
});
