// @vitest-environment happy-dom
/**
 * Motion only for a real change, and every motion has a still (agents-home spec §5.2), on Live's
 * own surfaces: the card that arrives rises in once (the ones there at first paint do not), at
 * most eight faces move at once, reduced motion calls no animation at all, and the stylesheet
 * gives every Live animation its still.
 */
import { readFileSync } from "node:fs";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { board, gate, mission } from "./_helpers/live-board-fixture";
import { $, $$, click, mount, rerender, stateOf, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { LiveBoardView } = await import("../app/agents/live-board");
const noop = () => {};
const view = (body: Record<string, unknown>) => (
  <LiveBoardView projectId="p1" actor="ceo" live={stateOf(body)} onOpenMap={noop} onActivity={noop}
                 onDirectory={noop} onNewAgent={noop} />
);
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("motion has a still", () => {
  it("cards at first paint do not rise in; one that arrives while you watch does", async () => {
    await mount(view(board({ attention: [gate()] })));
    expect($('[data-testid="ag-att-dec_gate"]')?.dataset.enter).toBeUndefined();
    await rerender(view(board({ attention: [gate(), gate({ id: "dec_new", decision_id: "dec_new" })] })));
    expect($('[data-testid="ag-att-dec_new"]')?.dataset.enter).toBe("true");
    expect($('[data-testid="ag-att-dec_gate"]')?.dataset.enter).toBeUndefined();
  });

  it("at most eight faces move: with twelve working, four hold still", async () => {
    const working = Array.from({ length: 12 }, (_, i) => mission({ run_id: `w${i}` }));
    await mount(view(board({ attention: [], missions: working })));
    await click($('[data-testid="ag-mission-more"]'));
    const rows = $$(".ag-mission");
    expect(rows).toHaveLength(12);
    expect(rows.filter((r) => r.dataset.still)).toHaveLength(4);
    expect(rows.slice(0, 8).every((r) => !r.dataset.still)).toBe(true);
  });

  it("cards count against the same eight", async () => {
    const cards = Array.from({ length: 4 }, (_, i) => gate({ id: `d${i}`, decision_id: `d${i}` }));
    const working = Array.from({ length: 6 }, (_, i) => mission({ run_id: `w${i}` }));
    await mount(view(board({ attention: cards, missions: working })));
    expect($$(".ag-mission").filter((r) => r.dataset.still)).toHaveLength(2);
  });

  it("under reduced motion nothing on Live calls an animation", async () => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("reduce"), media: q,
      addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
    const animate = vi.fn();
    const proto = Element.prototype as unknown as { animate?: unknown };
    const before = proto.animate;
    proto.animate = animate;
    try {
      await mount(view(board()));
      await rerender(view(board({ attention: [gate(), gate({ id: "dec_new", decision_id: "dec_new" })] })));
      expect(animate).not.toHaveBeenCalled();
    } finally {
      proto.animate = before;
    }
  });

  it("every Live animation has its still in the stylesheet", () => {
    const css = readFileSync("app/agents/agent-world.css", "utf8");
    const section = css.slice(css.indexOf("/* ── Agents home: Live (S2)"));
    const reduced = section.slice(section.lastIndexOf("@media (prefers-reduced-motion: reduce)"));
    for (const sel of [".ag-att[data-enter]", ".ag-att[data-pulse]", ".ag-spin", ".ag-chain-edge[data-new=\"true\"]"]) {
      expect(reduced, sel).toContain(sel);
    }
    // Past the budget, a face's ring holds still whatever its state.
    expect(section).toContain(".ag-mission[data-still] .ag-av");
  });
});
