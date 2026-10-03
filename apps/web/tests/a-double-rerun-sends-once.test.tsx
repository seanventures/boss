// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The lane and the editor are other tests' business; they get stand-ins here.
vi.mock("../app/workflows/studio-parts", () => import("./_helpers/studio-parts-stub"));

import { TOAST_EVENT } from "../lib/api-write";

/**
 * A double click on "Run this workflow again" starts ONE run and says nothing.
 *
 * Review of 6ca9119: Re-run put `again:<run>:<crypto.randomUUID()>` into its payload, and the
 * payload is what a press is recognised by — so the second click was a DIFFERENT press: refused,
 * and toasted "Re-run was not sent — the previous Re-run is still in progress", which reads as a
 * failure and contradicts the rule every other button keeps. The key is now minted from the
 * press itself. This mounts the real panel against a fake API and clicks the real button twice.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { DeliveryWorkflows } = await import("../app/delivery-workflows-ui");

const finished = {
  id: "wfr_done", status: "completed", standing: "closed", workflow_key: "standard_product_delivery",
  workflow_name: "Standard Product Delivery", version: 1, step_count: 1, steps_done: 1,
  current_step: null, waiting_on: null, blocked_reason: null, error: null, updated_at: null,
  input: { title: "Lead triage" }, finished: true,
  actions: [{ key: "rerun", label: "Run this workflow again", enabled: true, reason: null }],
  steps: [{ step_id: "build", kind: "capability", status: "succeeded", capability: "build",
            output: {}, evidence: [], error: null, approval: {}, due_at: null,
            timeout_applied: null, attempt: 1, started_at: null, completed_at: null }],
};
const VIEWS: Record<string, unknown> = {
  capabilities: { capabilities: [], blocks: [], manual_approval_risk_floor: 2 },
  catalogue: { items: [] }, installed: { items: [] },
  custom_steps: { steps: [], bindable: { skills: [], mcp_servers: [] } },
  schedules: { schedules: [] },
};

let starts: Array<{ body: Record<string, unknown>; key: string | null }> = [];
let release: () => void = () => {};
let root: Root | null = null;
let host: HTMLElement | null = null;
const toasts: string[] = [];
const onToast = (e: Event) => toasts.push((e as CustomEvent<{ message: string }>).detail.message);

beforeEach(() => {
  starts = []; toasts.length = 0;
  window.addEventListener(TOAST_EVENT, onToast);
  vi.stubGlobal("confirm", () => true);
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    if (method === "GET") {
      const view = new URL(String(input), "http://console.test").searchParams.get("view") ?? "";
      if (view === "runs") return Response.json({ runs: [finished] });
      return Response.json(VIEWS[view] ?? {});
    }
    const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
    if (body.action === "start") {
      starts.push({ body, key: new Headers(init?.headers).get("Idempotency-Key") });
      await new Promise<void>((resolve) => { release = resolve; });
      return Response.json({ run: { id: "wfr_new" } });
    }
    return Response.json({ ok: true, requeued: [] });
  }));
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  window.removeEventListener(TOAST_EVENT, onToast);
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 20; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

describe("Run this workflow again, pressed twice", () => {
  it("sends one start, with a dedupe key minted from the press, and shows no refusal", async () => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root!.render(<DeliveryWorkflows projectId="p1" actor="ceo" />));
    await settle();
    const button = host.querySelector<HTMLButtonElement>('[data-testid="dw-rerun"]');
    expect(button, "the finished run offers Run again").not.toBeNull();
    await act(async () => { button!.click(); button!.click(); });
    await settle();
    expect(starts).toHaveLength(1);
    expect(String(starts[0].body.dedupe_key)).toBe(`again:wfr_done:${starts[0].key}`);
    release();
    await settle();
    expect(toasts.filter((t) => /not sent|still in progress/.test(t))).toEqual([]);
  });
});
