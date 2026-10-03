// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../app/workflows/studio-parts", () => import("./_helpers/studio-parts-stub"));
// Who works the run is another test's business; the chip only has to swap it in and out.
vi.mock("../app/agents-on-this", () => ({ AgentsOnThis: () => null }));

import * as stub from "./_helpers/studio-parts-stub";
import type { Spec } from "../lib/workflow-editor-types";

/**
 * The notch under a run's lane points at the step it opened, after the lane moves too.
 *
 * Review of ws/ia (2026-10-03): the notch was measured when a step was picked and when the
 * window resized. When the run's saved workflow arrived and replaced the fallback lane, the
 * nodes moved and the notch kept the old x. Toggling the Agents chip off mounted a fresh panel
 * with no position at all. This mounts the real run view over the stand-in lane and lays nodes
 * out by their position in it.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { DeliveryWorkflows } = await import("../app/delivery-workflows-ui");

const step = (step_id: string, status: string) => ({
  step_id, kind: "capability", status, capability: step_id, output: {}, evidence: [], error: null,
  approval: {}, due_at: null, timeout_applied: null, attempt: 1, started_at: null,
  completed_at: null,
});
const live = {
  id: "wfr_live", status: "running", standing: "active", workflow_key: "mine",
  workflow_name: "Mine", version: 2, step_count: 2, steps_done: 1, current_step: "build",
  waiting_on: null, blocked_reason: null, error: null, updated_at: null,
  input: { title: "Lead triage" }, finished: false, actions: [],
  steps: [step("research", "succeeded"), step("build", "running")],
};
const VIEWS: Record<string, unknown> = {
  capabilities: { capabilities: [], blocks: [], manual_approval_risk_floor: 2 },
  catalogue: { items: [] }, installed: { items: [] },
  custom_steps: { steps: [], bindable: { skills: [], mcp_servers: [] } },
  schedules: { schedules: [] },
};

// A node sits 100px after the one before it in the lane and is 80px wide; the panel spans
// 0..1000. So a step's notch is at 100 × (its place in the lane) + 40.
const realRect = Element.prototype.getBoundingClientRect;
function laidOut(this: Element): DOMRect {
  if (this instanceof HTMLElement && this.dataset.stepId) {
    const nodes = [...(this.parentElement?.querySelectorAll("[data-step-id]") ?? [])];
    return new DOMRect(nodes.indexOf(this) * 100, 0, 80, 40);
  }
  if (this.classList.contains("dw-detail")) return new DOMRect(0, 60, 1000, 300);
  return realRect.call(this);
}

let root: Root | null = null;
let host: HTMLElement | null = null;
let publish: (spec: Spec) => void = () => {};

beforeEach(() => {
  Element.prototype.getBoundingClientRect = laidOut;
  stub.fetchSpec.mockImplementation(() => new Promise((resolve) => {
    publish = (spec) => resolve({ spec, meta: undefined as never, versions: [] });
  }));
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const view = new URL(String(input), "http://console.test").searchParams.get("view") ?? "";
    if (view === "runs") return Response.json({ runs: [live] });
    return Response.json(VIEWS[view] ?? {});
  }));
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  Element.prototype.getBoundingClientRect = realRect;
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 20; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

async function mount() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<DeliveryWorkflows projectId="p1" actor="ceo" />));
  await settle();
}

const arrow = () => host!.querySelector<HTMLElement>(".dw-detail-wrap")?.style
  .getPropertyValue("--dw-arrow-x");

describe("the callout notch", () => {
  it("moves with its step when the run's saved workflow replaces the fallback lane", async () => {
    await mount();
    // The fallback lane: research, build. The run opens on build, the step in flight.
    expect(arrow()).toBe("140px");

    // The saved workflow draws a check between them, so build is now third.
    await act(async () => publish({ steps: [
      { id: "research", kind: "capability" }, { id: "check", kind: "gate" },
      { id: "build", kind: "capability" },
    ] }));
    await settle();
    expect(host!.querySelectorAll('[data-testid="flow-lane"] [data-step-id]')).toHaveLength(3);
    expect(arrow()).toBe("240px");
  });

  it("is placed again on the panel that comes back when Agents is toggled off", async () => {
    await mount();
    const agents = host!.querySelector<HTMLButtonElement>('[data-testid="dw-run-agents"]')!;
    await act(async () => agents.click());
    await settle();
    expect(host!.querySelector(".dw-detail-wrap")).toBeNull();
    await act(async () => agents.click());
    await settle();
    expect(arrow()).toBe("140px");
  });
});
