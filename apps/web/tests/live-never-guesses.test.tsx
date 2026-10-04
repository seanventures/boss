// @vitest-environment happy-dom
/**
 * Live never guesses (agents-home spec §1.3, §4.3): no percent, no ETA, no estimate; an item
 * the server attributes to nobody shows the BOSS glyph or You, never an agent's face; and a
 * cost shows only when there is one.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { board, failed, gate, mission, prReview } from "./_helpers/live-board-fixture";
import { $, $$, mount, stateOf, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { LiveBoardView } = await import("../app/agents/live-board");
const noop = () => {};
const view = (body: Record<string, unknown>) => (
  <LiveBoardView projectId="p1" actor="ceo" live={stateOf(body)} onOpenMap={noop} onActivity={noop}
                 onDirectory={noop} onNewAgent={noop} />
);
afterEach(() => unmount());

describe("Live never guesses", () => {
  it("says no percent, no ETA and no estimate anywhere — text or tooltip", async () => {
    await mount(view(board({ attention: [gate(), failed(), prReview()],
                             missions: [mission(), mission({ run_id: "w2", state: "waiting", waiting_on: "build to finish" }),
                                        mission({ run_id: "w3", state: "done" })] })));
    const tips = $$("[title], [data-tip]").map((e) => `${e.getAttribute("title") ?? ""} ${e.getAttribute("data-tip") ?? ""}`);
    const all = `${document.body.textContent} ${tips.join(" ")}`;
    expect(all).not.toMatch(/%/);
    expect(all).not.toMatch(/\bETA\b/i);
    expect(all).not.toMatch(/\bestimat/i);
    expect(all).not.toMatch(/almost done/i);
  });

  it("an item attributed to nobody shows the BOSS glyph, never an agent face or a person", async () => {
    await mount(view(board({ attention: [
      prReview(),
      { id: "ready:repo", kind: "readiness", title: "Connect a repository", detail: "", role_key: null,
        actions: [], answers: [], decide_inline: false, fix: { area: "setup", label: "Fix" } },
    ] })));
    const variants = (id: string) => [...$(`[data-testid="ag-att-${id}"] .ag-att-face`)!.querySelectorAll(".ag-avatar")]
      .map((a) => (a as HTMLElement).dataset.variant);
    expect(variants("dec_pr")).toEqual(["boss"]);
    // Setup is a line to Setup now, not a card (walk D2).
    expect($('[data-testid="ag-att-ready:repo"]')).toBeNull();
    expect($('[data-testid="ag-live-setup-ready:repo"]')).not.toBeNull();
    // The attributed one is the agent's own face.
    unmount();
    await mount(view(board()));
    expect([...$('[data-testid="ag-att-dec_gate"] .ag-att-face')!.querySelectorAll<HTMLElement>(".ag-avatar")]
      .map((a) => [a.dataset.variant, a.dataset.roleKey])).toEqual([["agent", "media_publisher"]]);
  });

  it("a cost shows only when there is one, as metered spend", async () => {
    await mount(view(board()));
    const priced = $('[data-testid="ag-mission-wfr_182"] .ag-mission-cost')!;
    expect(priced.textContent).toBe("$0.42");
    expect(priced.title).toBe("Metered model spend so far");
    expect($('[data-testid="ag-mission-wfr_video"] .ag-mission-cost')?.textContent).toBe("");
  });

  it("step k of n says it is steps done, not a forecast", async () => {
    await mount(view(board()));
    const step = $('[data-testid="ag-mission-wfr_182"] .ag-mission-step')!;
    expect(step.textContent).toBe("step 4 of 7");
    expect(step.title).toBe("Steps done so far. BOSS doesn't guess finish times.");
  });

  it("upcoming faces are only the ones the workflow names, hollow, with what they will do", async () => {
    await mount(view(board()));
    const next = $$('[data-testid="ag-mission-wfr_182"] .ag-mission-next');
    expect(next.map((n) => n.title)).toEqual(["Next: Vera · Test"]);
  });
});

describe("a gate is drawn once", () => {
  it("drops the server's gate hop; the lane's own You hop stands for it", async () => {
    const { parseMission } = await import("../lib/agents-live-board");
    const m = parseMission({ run_id: "r", state: "waiting_you", hops: [
      { act_id: "a1", role_key: "pm", state: "done", label: "PRD" },
      { act_id: "a2", role_key: null, person: "ceo", state: "needs_you", label: "Gate", gate: true },
    ] });
    expect(m.hops.map((h) => (h.kind === "hop" ? h.actId : h.kind))).toEqual(["a1"]);
  });
});

