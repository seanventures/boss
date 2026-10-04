// @vitest-environment happy-dom
/**
 * Agents › Live says what needs you first (agents-home spec §4.1, §4.2): NEEDS YOU, LIVE WORK,
 * DONE LATELY, AGENT FORCE, in that order. A card draws exactly the server's actions, decides
 * in place only when the server allows it, asks before a reject, and says the server's own
 * sentence when the decision was refused.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ActionResult } from "../lib/action-result";
import { board, gate, prReview } from "./_helpers/live-board-fixture";
import { $, $$, click, mount, stateOf, unmount } from "./_helpers/live-render";

const decide = vi.fn<(form: FormData) => Promise<ActionResult>>();
vi.mock("../app/actions", async (original) => ({
  ...(await original<typeof import("../app/actions")>()),
  resolveDecisionAction: (form: FormData) => decide(form),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { LiveBoardView } = await import("../app/agents/live-board");

const noop = () => {};
const view = (body = board()) => (
  <LiveBoardView projectId="p1" actor="ceo" live={stateOf(body)} onOpenMap={noop} onActivity={noop}
                 onDirectory={noop} onNewAgent={noop} />
);

let toasts: string[] = [];
const onToast = (e: Event) => toasts.push((e as CustomEvent<{ message: string }>).detail.message);
beforeEach(() => {
  decide.mockReset();
  toasts = [];
  window.addEventListener("boss:toast", onToast);
  window.history.replaceState(null, "", "/?area=workforce");
});
afterEach(() => { window.removeEventListener("boss:toast", onToast); unmount(); });

const card = (id: string) => $(`[data-testid="ag-att-${id}"]`)!;

describe("Live, top to bottom", () => {
  it("is NEEDS YOU, LIVE WORK, DONE LATELY, AGENT FORCE", async () => {
    await mount(view());
    expect($$(".ag-live-head h2").map((h) => h.textContent)).toEqual(["NEEDS YOU", "LIVE WORK", "DONE LATELY", "AGENT FORCE"]);
    for (const id of ["ag-live-needs", "ag-live-work", "ag-live-done", "ag-live-force"]) {
      expect($(`#${id}`)?.getAttribute("role")).toBe("region");
    }
    expect($$('[data-testid^="ag-mission-wfr"]').length).toBe(3);
    expect($$('[data-testid="ag-outcome"]').map((o) => o.textContent)).toEqual(
      expect.arrayContaining([expect.stringContaining("PR #842 opened"), expect.stringContaining("Minutes ready")]));
  });

  it("a waiting gate comes first in its section, the failed step after it", async () => {
    await mount(view());
    expect($$("[data-att-card]").map((c) => c.dataset.attId)).toEqual(["dec_gate", "run:wfr_ship:build"]);
  });
});

describe("the attention card", () => {
  it("draws exactly the server's actions, with their effects as tooltips", async () => {
    await mount(view());
    const buttons = [...card("dec_gate").querySelectorAll<HTMLButtonElement>(".ag-att-actions .ag-btn")];
    expect(buttons.map((b) => b.textContent)).toEqual(["Publish it", "Reject"]);
    expect(buttons[0].title).toBe("Releases the step and the workflow continues from it.");
  });

  it("decide_inline false: only Review ↗, into Approvals", async () => {
    await mount(view(board({ attention: [prReview()] })));
    const c = card("dec_pr");
    expect(c.querySelector('[data-testid="ag-att-approve"]')).toBeNull();
    const review = c.querySelector<HTMLAnchorElement>('[data-testid="ag-att-review"]')!;
    expect(review.textContent).toBe("Review ↗");
    expect(review.title).toBe("Needs a closer look in Approvals");
    expect(review.getAttribute("href")).toContain("area=decisions&decision=dec_pr");
  });

  it("approves through the inbox's own action, with the preview's hash and the run", async () => {
    decide.mockResolvedValue({ ok: true });
    await mount(view());
    await click(card("dec_gate").querySelector('[data-testid="ag-att-approve"]'));
    expect(decide).toHaveBeenCalledTimes(1);
    const form = decide.mock.calls[0][0];
    expect(Object.fromEntries(form.entries())).toMatchObject({
      decision_id: "dec_gate", action: "approve", actor: "ceo", project_id: "p1", content_hash: "h1",
      workflow_run_id: "wfr_video",
    });
    expect(toasts).toContain("Approved · Weekly video continues");
    expect(card("dec_gate").dataset.leaving).toBe("true");
  });

  it("a reject asks first, then fails the step", async () => {
    decide.mockResolvedValue({ ok: true });
    await mount(view());
    await click(card("dec_gate").querySelector('[data-testid="ag-att-reject"]'));
    expect(decide).not.toHaveBeenCalled();
    expect($('[data-testid="ag-att-confirm"]')?.textContent).toContain("Reject this?");
    await click($('[data-testid="ag-att-confirm-reject"]'));
    expect(decide.mock.calls[0][0].get("action")).toBe("reject");
    expect(toasts).toContain("Rejected · run stopped");
  });

  it("a refusal (409) shows the server's sentence on the card", async () => {
    decide.mockResolvedValue({ ok: false, error: "Already decided by ceo." });
    await mount(view());
    await click(card("dec_gate").querySelector('[data-testid="ag-att-approve"]'));
    expect(card("dec_gate").querySelector('[data-testid="ag-att-error"]')?.textContent).toBe("Already decided by ceo.");
    expect(card("dec_gate").dataset.leaving).toBeUndefined();
  });

  it("A and R work only inside a card that can decide in place", async () => {
    decide.mockResolvedValue({ ok: true });
    await mount(view(board({ attention: [gate(), prReview()] })));
    const { act } = await import("react");
    await act(async () => { card("dec_pr").dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true })); });
    expect(decide).not.toHaveBeenCalled();
    await act(async () => { card("dec_gate").dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true })); });
    expect(decide).toHaveBeenCalledTimes(1);
    expect(card("dec_gate").getAttribute("aria-describedby")).toBeTruthy();
  });

  it("Details answers why, what each answer does, what it blocks and the risk", async () => {
    await mount(view());
    await click(card("dec_gate").querySelector('[data-testid="ag-att-details"]'));
    const dts = [...card("dec_gate").querySelectorAll(".ag-att-details dt")].map((d) => d.textContent);
    expect(dts).toEqual(["Why", "If approved", "If rejected", "Blocks", "Preview", "Risk", "Approvals"]);
    expect(card("dec_gate").textContent).toContain("Tier 2");
    expect(card("dec_gate").textContent).toContain("Open in Approvals ↗");
  });

  it("past four cards, +n more opens the rest in place", async () => {
    const many = Array.from({ length: 6 }, (_, i) => gate({ id: `dec_${i}`, decision_id: `dec_${i}` }));
    await mount(view(board({ attention: many })));
    expect($$("[data-att-card]")).toHaveLength(4);
    expect($('[data-testid="ag-att-more"]')?.textContent).toBe("+2 more");
    await click($('[data-testid="ag-att-more"]'));
    expect($$("[data-att-card]")).toHaveLength(6);
  });
});
