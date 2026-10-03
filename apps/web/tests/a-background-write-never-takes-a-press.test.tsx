// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The lane and the editor are other tests' business; they get stand-ins here.
vi.mock("../app/workflows/studio-parts", () => import("./_helpers/studio-parts-stub"));

import { TOAST_EVENT } from "../lib/api-write";

/**
 * The delivery panel's automatic recheck is not a press, and a press is never dropped silently.
 *
 * Review of 45e7693: the panel sent every write through one press scope, the automatic recheck
 * included. The recheck sends one request per stopped run, all at once; the first took the scope
 * and every other run got "already working" and was marked as rechecked anyway — so of several
 * runs whose blocker had cleared, only the first was put back and the rest stayed stuck while the
 * page was open. And a Retry pressed while a recheck ran was refused by an early return the
 * button threw away: "I clicked and nothing happened", the complaint this console keeps hearing.
 *
 * These mount the real panel against a fake API and press its real buttons.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { DeliveryWorkflows } = await import("../app/delivery-workflows-ui");

type Sent = { url: string; method: string; body: Record<string, unknown> };

function stoppedRun(id: string) {
  return {
    id, status: "deadlettered", workflow_key: "standard_product_delivery",
    workflow_name: "Standard Product Delivery", version: 1, step_count: 1, steps_done: 0,
    current_step: "deploy", waiting_on: null, blocked_reason: null, error: null,
    updated_at: null, input: { title: `App ${id}` }, finished: false,
    actions: [{ key: "retry_step", label: "Try again", enabled: true, reason: null,
                step_id: "deploy" }],
    steps: [{
      step_id: "deploy", kind: "capability", status: "deadlettered", capability: "deploy",
      output: {}, evidence: [], error: "no deployment target", approval: {}, due_at: null,
      timeout_applied: null, attempt: 1, started_at: null, completed_at: null,
      // The read path's verdict: what stopped this step has since been dealt with.
      blocker: { cleared: true },
    }],
  };
}

const VIEWS: Record<string, unknown> = {
  capabilities: { capabilities: [], blocks: [], manual_approval_risk_floor: 2 },
  catalogue: { items: [] },
  installed: { items: [] },
  custom_steps: { steps: [], bindable: { skills: [], mcp_servers: [] } },
  schedules: { schedules: [] },
};

let sent: Sent[] = [];
let held: { action: string; release: () => void }[] = [];
let holdActions = new Set<string>();
let root: Root | null = null;
let host: HTMLElement | null = null;
const toasts: string[] = [];
const onToast = (e: Event) => toasts.push((e as CustomEvent<{ message: string }>).detail.message);

beforeEach(() => {
  sent = []; held = []; holdActions = new Set(["recheck"]); toasts.length = 0;
  window.addEventListener(TOAST_EVENT, onToast);
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (method === "GET") {
      const view = new URL(url, "http://console.test").searchParams.get("view") ?? "";
      if (view === "runs") return Response.json({ runs: ["wfr_a", "wfr_b", "wfr_c"].map(stoppedRun) });
      return Response.json(VIEWS[view] ?? {});
    }
    const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
    sent.push({ url, method, body });
    const action = String(body.action ?? "");
    if (holdActions.has(action)) {
      await new Promise<void>((resolve) => held.push({ action, release: resolve }));
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

async function mount() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<DeliveryWorkflows projectId="p1" actor="ceo" />));
  await settle();
}

const posts = (action: string) => sent.filter((s) => s.body.action === action);

describe("the automatic recheck is a background write, not a press", () => {
  it("puts back every stopped run at once — all three rechecks reach the API together", async () => {
    await mount();
    // All three are on the wire at the same time, none refused by another.
    expect(posts("recheck").map((s) => s.body.run_id).sort()).toEqual(["wfr_a", "wfr_b", "wfr_c"]);
    expect(held.length).toBe(3);
    held.forEach((h) => h.release());
    await settle();
  });

  it("does not stop the operator pressing Try again while it runs", async () => {
    await mount();
    expect(held.length).toBe(3); // three rechecks still in flight
    const retry = host!.querySelector<HTMLButtonElement>('[data-testid="dw-run-stopped-retry"]');
    expect(retry, "the stopped run offers Try again").not.toBeNull();
    await act(async () => { retry!.click(); });
    await settle();
    expect(posts("retry_step").length).toBe(1);
    expect(toasts).toEqual([]);
    held.forEach((h) => h.release());
    await settle();
  });
});

describe("a refused press is said, in the operator's words", () => {
  it("a second, different press while one runs is announced naming what is running", async () => {
    holdActions = new Set(["retry_step"]);
    await mount();
    const buttons = [...host!.querySelectorAll<HTMLButtonElement>('[data-testid="dw-run-stopped-retry"]')];
    expect(buttons.length).toBe(3);
    // Two Try again presses on two different runs, in the same tick — before `busy` re-renders.
    await act(async () => { buttons[0].click(); buttons[1].click(); });
    await settle();
    expect(posts("retry_step").length).toBe(1);
    expect(toasts).toEqual([
      "Retry was not sent — the previous Retry is still in progress. Try again when it finishes.",
    ]);
    held.forEach((h) => h.release());
    await settle();
  });

  it("the same press twice (a double click) sends once and says nothing", async () => {
    holdActions = new Set(["retry_step"]);
    await mount();
    const retry = host!.querySelector<HTMLButtonElement>('[data-testid="dw-run-stopped-retry"]')!;
    await act(async () => { retry.click(); retry.click(); });
    await settle();
    expect(posts("retry_step").length).toBe(1);
    expect(toasts).toEqual([]);
    held.forEach((h) => h.release());
    await settle();
  });
});
