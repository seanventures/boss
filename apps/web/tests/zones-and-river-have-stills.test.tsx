// @vitest-environment happy-dom
/**
 * Motion only on a real state change, and a still for every motion (agents-home spec §5.2, §7)
 * — for Organization, Agent force and Activity:
 *   - reduced motion: a focused run's path lights ALL at once, with no timers and no animate();
 *   - idle faces in the compact view are drawn still;
 *   - at most 8 faces move at once, however many agents are working;
 *   - the river's faces never move.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SHIP_IT_MISSION, TEAM_WIRE, board, stubMatchMedia } from "./_helpers/agents-home-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { TeamZones } = await import("../app/agents/team-zones");
const { ActivityRiver } = await import("../app/agents/activity-river");

let root: Root | null = null;
let host: HTMLElement | null = null;
const animate = vi.fn();
beforeEach(() => {
  (Element.prototype as unknown as { animate: unknown }).animate = animate;
  animate.mockClear();
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    if (String(input).includes("/v1/agents/activity")) {
      return Response.json({ items: [{ act_id: "a1", role_key: "qa", kind: "step", status: "active", how: "named",
                                       edge_label: "", label: "Test · Ship it", verb: "Testing",
                                       started_at: new Date().toISOString() }], next: null });
    }
    return Response.json({});
  }));
});
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.body.innerHTML = "";
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
async function render(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  for (let i = 0; i < 8; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
const $$ = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)];

describe("stills", () => {
  it("under reduced motion a focused run lights every hop at once, with no animation", async () => {
    stubMatchMedia(["prefers-reduced-motion"]);
    await render(<TeamZones projectId="p1" board={board({}, { missions: [SHIP_IT_MISSION] })} density="full" focusRunId="wfr_1" />);
    expect($$(".ag-zones-agent[data-lit]")).toHaveLength(3);
    expect($$(".ag-zones-edge")).toHaveLength(2);
    expect(animate).not.toHaveBeenCalled();
  });

  it("with motion, the path lights one hop at a time", async () => {
    stubMatchMedia([]);
    await render(<TeamZones projectId="p1" board={board({}, { missions: [SHIP_IT_MISSION] })} density="full" focusRunId="wfr_1" />);
    expect($$(".ag-zones-agent[data-lit]").length).toBeLessThan(3);
    await act(async () => { await new Promise((r) => setTimeout(r, 900)); });
    expect($$(".ag-zones-agent[data-lit]")).toHaveLength(3);
    expect($$(".ag-zones-edge[data-new]")).toHaveLength(2);
  });

  it("idle faces in the compact view are still; at most 8 faces move with 12 working", async () => {
    stubMatchMedia([]);
    const twelve = Object.fromEntries([...TEAM_WIRE.leadership, ...TEAM_WIRE.quality, ...TEAM_WIRE.cost.slice(0, 1)]
      .map((k) => [k, ["working", "Now: Working"]])) as Record<string, [string, string]>;
    expect(Object.keys(twelve)).toHaveLength(12);
    await render(<TeamZones projectId="p1" board={board(twelve)} density="compact" />);
    for (const f of $$(".ag-team-face[data-state='idle']")) expect(f.dataset.still).toBe("true");
    const moving = $$(".ag-team-face").filter((f) => f.dataset.still === undefined);
    expect(moving.length).toBeLessThanOrEqual(8);
    expect(moving.every((f) => f.dataset.state === "working")).toBe(true);
    // An all-idle team is a pile of still faces with no animation hook at all.
    expect($$('[data-team="engineering"] .ag-team-face')).toHaveLength(0);
  });

  it("the river's faces never move", async () => {
    await render(<ActivityRiver projectId="p1" />);
    const face = document.querySelector<HTMLElement>(".ag-river-face")!;
    expect(face.hasAttribute("data-still")).toBe(true);
    expect(animate).not.toHaveBeenCalled();
  });
});
