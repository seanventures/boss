// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * A re-run the project cannot finish is refused WITH the checklist and "Re-run anyway" — and
 * "Stop and run again" never stops the run it was going to replace when the start is refused.
 *
 * Review of the readiness change (2026-10-02): Re-run and Build on this research showed a line
 * of text and no way past; Stop and run again stopped the live run first and then the start was
 * refused, so the operator was left with no run at all. This mounts the real panel against a
 * fake API that refuses the first start the way the API does (409, `{error, readiness}` after
 * the route flattens it).
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;
const { DeliveryWorkflows } = await import("../app/delivery-workflows-ui");

const READINESS = {
  ready: false,
  checked: [{ key: "deployment_target" }],
  missing: [{ key: "deployment_target", label: "Somewhere to deploy: a verified deployment host",
              fix: "Add and verify a host in Infra → Where apps install.", area: "delivery",
              ok: false, detail: "this project has no verified deployment host",
              needed_by: [{ step_id: "deploy", label: "Deploy" }], first_step_label: "Deploy",
              after_a_person: true }],
};

function run(over: Record<string, unknown>) {
  return {
    id: "wfr_done", status: "completed", standing: "closed",
    workflow_key: "standard_product_delivery", workflow_name: "Standard Product Delivery",
    version: 1, step_count: 1, steps_done: 1, current_step: null, waiting_on: null,
    blocked_reason: null, error: null, updated_at: null, input: { title: "Lead triage" },
    finished: true,
    actions: [{ key: "rerun", label: "Run this workflow again", enabled: true, reason: null }],
    steps: [{ step_id: "build", kind: "capability", status: "succeeded", capability: "build",
              output: {}, evidence: [], error: null, approval: {}, due_at: null,
              timeout_applied: null, attempt: 1, started_at: null, completed_at: null }],
    ...over,
  };
}

const VIEWS: Record<string, unknown> = {
  capabilities: { capabilities: [], blocks: [], manual_approval_risk_floor: 2 },
  catalogue: { items: [] }, installed: { items: [] },
  custom_steps: { steps: [], bindable: { skills: [], mcp_servers: [] } },
  schedules: { schedules: [] },
};

let posts: Array<Record<string, unknown>> = [];
let runs: unknown[] = [];
let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => {
  posts = [];
  vi.stubGlobal("confirm", () => true);
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    if (method === "GET") {
      const view = new URL(String(input), "http://console.test").searchParams.get("view") ?? "";
      if (view === "runs") return Response.json({ runs });
      return Response.json(VIEWS[view] ?? {});
    }
    const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
    posts.push(body);
    if (body.action === "start" && !body.start_anyway) {
      return Response.json({ error: "This workflow cannot run here yet; 1 thing is missing.",
                             readiness: READINESS }, { status: 409 });
    }
    if (body.action === "start") return Response.json({ run: { id: "wfr_new" } });
    return Response.json({ ok: true, requeued: [] });
  }));
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

async function mount() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<DeliveryWorkflows projectId="p1" actor="ceo" />));
  await settle();
  return host;
}

describe("a refused re-run", () => {
  it("draws the checklist, and Re-run anyway sends exactly the ticked keys", async () => {
    runs = [run({})];
    const page = await mount();
    const rerun = page.querySelector<HTMLButtonElement>('[data-testid="dw-rerun"]');
    expect(rerun).not.toBeNull();
    await act(async () => { rerun!.click(); });
    await settle();

    const refusal = page.querySelector('[data-testid="start-refusal"]');
    expect(refusal, "the refusal draws the checklist").not.toBeNull();
    expect(refusal!.textContent).toContain("Somewhere to deploy");
    expect(refusal!.textContent).toContain("Add and verify a host in Infra");
    const anyway = page.querySelector<HTMLButtonElement>('[data-testid="start-refusal-anyway"]');
    expect(anyway!.disabled, "nothing accepted yet").toBe(true);

    const tick = page.querySelector<HTMLInputElement>('[data-testid="dw-readiness-start-anyway"]');
    await act(async () => { tick!.click(); });
    expect(anyway!.disabled).toBe(false);
    await act(async () => { anyway!.click(); });
    await settle();

    const starts = posts.filter((p) => p.action === "start");
    expect(starts).toHaveLength(2);
    expect(starts[0].rerun_of).toBe("wfr_done");
    expect(starts[1].start_anyway).toEqual(["deployment_target"]);
    expect(starts[1].rerun_of).toBe("wfr_done");
    expect(page.querySelector('[data-testid="start-refusal"]')).toBeNull();
  });
});

describe("a refused Stop and run again", () => {
  it("is one start naming the run it replaces — no stop is sent from the browser", async () => {
    runs = [run({
      id: "wfr_live", status: "running", standing: "running", finished: false,
      actions: [{ key: "stop_and_rerun", label: "Stop and run again", enabled: true,
                  reason: null }],
    })];
    const page = await mount();
    const button = page.querySelector<HTMLButtonElement>('[data-testid="dw-stop-and-rerun"]');
    expect(button, "the live run offers Stop and run again").not.toBeNull();
    await act(async () => { button!.click(); });
    await settle();
    expect(posts.map((p) => p.action)).toEqual(["start"]);
    expect(posts[0].replaces_run).toBe("wfr_live");
    expect(page.querySelector('[data-testid="start-refusal"]')).not.toBeNull();
  });
});
