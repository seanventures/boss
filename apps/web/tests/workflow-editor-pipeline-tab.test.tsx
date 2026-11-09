// @vitest-environment happy-dom
// @vitest-environment-options {"url": "http://console.test/?area=delivery_workflows&wf=standard_product_delivery"}

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fakeWorkflowApi, type FakeApi } from "./_helpers/fake-workflow-api";
import { linear, PALETTE } from "./_helpers/canvas-fixtures";

/**
 * KG P6b (round 2): the Pipeline tab is reachable from the real Workflow Studio editor, not
 * just from its own test — the review's BLOCKER was that pipeline-view.tsx was mounted nowhere
 * a page actually renders. This test opens the real editor, switches to the Pipeline tab, and
 * finds the stage diagram and a contract stage's captain switcher offering a connected outside
 * agent, exactly as an operator would.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { WorkflowEditor } = await import("../app/workflows/editor/workflow-editor");

const STANDARD = { definition_id: "wfd_std", workflow_key: "standard_product_delivery",
                   name: "Standard Product Delivery", is_catalogue: true, version: 3 };

const OUTSIDE_AGENT = {
  id: "ext_bank_1", project_id: "prj_1", display_name: "Acme Bank review agent",
  endpoint_url: "https://acme-bank.example.com/agent", secret_ref_id: "sr_1",
  sponsor_principal_id: "ceo", status: "active", created_by: "ceo",
  created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z",
};

let api: FakeApi;
let root: Root | null = null;
let host: HTMLElement | null = null;
const realClientWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");
const onBack = vi.fn();
const onStart = vi.fn();

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => 4000 });
  window.history.replaceState(null, "", "/?area=delivery_workflows&wf=standard_product_delivery");
  api = fakeWorkflowApi(PALETTE, [{ ...STANDARD, spec: linear(2) }]);
  const workflowFetch = api.fetch;
  vi.stubGlobal("fetch", vi.fn((input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    if (url.pathname === "/v1/external-agents" && (init?.method ?? "GET").toUpperCase() === "GET") {
      return Promise.resolve(Response.json({ items: [OUTSIDE_AGENT] }));
    }
    return workflowFetch(input, init);
  }));
  onBack.mockReset();
  onStart.mockReset();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(async () => {
  await new Promise((r) => setTimeout(r, 0));
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  vi.unstubAllGlobals();
  if (realClientWidth) Object.defineProperty(HTMLElement.prototype, "clientWidth", realClientWidth);
  else delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth;
});

async function settle(rounds = 8) {
  for (let i = 0; i < rounds; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

const byTest = (id: string) => document.querySelector(`[data-testid="${id}"]`) as HTMLElement;
const click = (el: Element) => act(() => (el as HTMLElement).click());

describe("the Pipeline tab in the real Workflow Studio editor", () => {
  it("renders the stage diagram and offers a connected outside agent as a stage's captain", async () => {
    act(() => root!.render(<WorkflowEditor projectId="prj_1" workflowKey="standard_product_delivery"
                                           onBack={onBack} onStart={onStart} actor="ops@example.test" />));
    await settle();

    const tab = byTest("wf-view-pipeline");
    expect(tab).not.toBeNull();
    click(tab);
    await settle();

    expect(byTest("wf-pipeline-tab")).not.toBeNull();
    expect(document.querySelector('[data-area="pipeline-view"]')).not.toBeNull();
    expect(byTest("stage-research")).not.toBeNull();

    // The switcher is on the stage in hand.
    click(byTest("stage-research"));
    await settle();
    const select = byTest("stage-captain-select-research") as HTMLSelectElement;
    expect(select).not.toBeNull();
    const outsideOption = [...select.options].find((o) => o.value === `outside:${OUTSIDE_AGENT.id}`);
    expect(outsideOption?.textContent).toContain("Acme Bank review agent");

    act(() => {
      select.value = `outside:${OUTSIDE_AGENT.id}`;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await settle();

    // One chip says who runs it; no second "Outside agent" badge.
    expect(byTest("outside-agent-badge")).toBeNull();
    expect(byTest("stage-runs-as-research")?.textContent).toContain("Outside agent");
    expect(byTest("outside-agent-host").textContent).toBe("acme-bank.example.com");
  });

  it("shows a contract stage's contract on its badge, the last run's status, and its read/write view", async () => {
    const withContract = {
      ...STANDARD,
      spec: linear(2, (i) => (i === 0 ? {
        captain: { external_agent: OUTSIDE_AGENT.id },
        contract: { reads: [{ kind: "rule" }], writes: [{ kind: "agent_decision", required: true }] },
      } : {})),
    };
    api = fakeWorkflowApi(PALETTE, [{
      ...withContract, last_run: { id: "wfr_1", status: "completed", at: "2026-10-30T00:00:00Z" },
    }], {
      // The runner's own word is "succeeded" — the old trace kept only "completed" and never filled.
      wfr_1: { status: "running", steps: {
        research: { status: "succeeded",
                    reads: [{ kind: "rule", count: 2, examples: ["Payments credit on receipt"], decision_types: [] }],
                    writes: [{ kind: "agent_decision", count: 1, examples: [], decision_types: ["Assessment"] }],
                    calls: [] },
        meeting: { status: "running", reads: [], writes: [], calls: [] },
      } },
    });
    const workflowFetch = api.fetch;
    vi.stubGlobal("fetch", vi.fn((input: unknown, init?: RequestInit) => {
      const url = new URL(String(input), "http://console.test");
      if (url.pathname === "/v1/external-agents" && (init?.method ?? "GET").toUpperCase() === "GET") {
        return Promise.resolve(Response.json({ items: [OUTSIDE_AGENT] }));
      }
      return workflowFetch(input, init);
    }));

    act(() => root!.render(<WorkflowEditor projectId="prj_1" workflowKey="standard_product_delivery"
                                           onBack={onBack} onStart={onStart} actor="ops@example.test" />));
    await settle();
    click(byTest("wf-view-pipeline"));
    await settle();

    // The same compact contract line as every other card, the kinds on hover.
    expect(byTest("stage-contract-research").textContent).toBe("reads 1 · writes 1");
    expect(byTest("stage-contract-research").getAttribute("title")).toBe("Reads Rule, Writes Agent decision");
    expect(byTest("stage-research").textContent).toContain("Succeeded");
    expect(byTest("stage-meeting").textContent).toContain("Running");
    expect(byTest("stage-row-research").textContent).toContain("Outside agent · Acme Bank review agent");

    click(byTest("stage-research"));
    await settle();
    expect(byTest("pipeline-stage-detail")).not.toBeNull();
    click(byTest("show-last-run"));
    const rule = document.querySelector('[data-testid="node-read-rule"]')!;
    expect(rule.getAttribute("data-overlay")).toBe("done");
    expect(document.querySelector('[data-testid="node-write-agentdecision"]')!.getAttribute("data-overlay")).toBe("done");
  });
});
