// @vitest-environment happy-dom
/**
 * Motion only on a real state change, and a still for every motion (agents-home spec §5.2, §7)
 * — for Organization, Agent force and Activity:
 *   - reduced motion: Flow lights a focused run's path ALL at once, with no animateMotion and no
 *     animate(); with motion it draws in hop by hop; a poll never moves a node;
 *   - idle faces in the compact view are drawn still;
 *   - at most 8 faces move at once, however many agents are working;
 *   - the river's faces never move.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TEAM_WIRE, board, stubMatchMedia } from "./_helpers/agents-home-fixtures";
import { orgWire } from "./_helpers/org-board-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { TeamZones } = await import("../app/agents/team-zones");
const { ActivityRiver } = await import("../app/agents/activity-river");
const { OrgView } = await import("../app/agents/org-view");

const CHAIN = { acts: [
  { act_id: "a0", role_key: null, kind: "run", status: "active", label: "Ship it #182", started_at: "2026-10-04T10:00:00Z" },
  { act_id: "a1", role_key: "pm", kind: "step", status: "complete", parent_act_id: "a0", label: "PRD", started_at: "2026-10-04T10:00:01Z" },
  { act_id: "a2", role_key: "chief_architect", kind: "step", status: "complete", parent_act_id: "a1", label: "Design", started_at: "2026-10-04T10:00:02Z" },
  { act_id: "a3", role_key: "software_engineer", kind: "step", status: "active", parent_act_id: "a2", label: "Build", started_at: "2026-10-04T10:00:03Z" },
] };
let boardWire = orgWire();

let root: Root | null = null;
let host: HTMLElement | null = null;
const animate = vi.fn();
beforeEach(() => {
  (Element.prototype as unknown as { animate: unknown }).animate = animate;
  animate.mockClear();
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    if (String(input).includes("/v1/workflow-runs/wfr_1/chain")) return Response.json(CHAIN);
    if (String(input).startsWith("/v1/agents/org-board")) return Response.json(boardWire);
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
    window.history.replaceState(null, "", "/?area=workforce&view=org&run=wfr_1");
    await render(<OrgView projectId="p1" live={new Map()} focusRunId="wfr_1" lines={() => <div />} />);
    expect($$(".ag-flow-agent[data-lit]")).toHaveLength(3);
    expect($$('path.ag-flow-edge[data-level="agent"]')).toHaveLength(2);
    expect(document.querySelector("animateMotion")).toBeNull();
    expect(animate).not.toHaveBeenCalled();
  });

  it("with motion, the path lights one hop at a time", async () => {
    stubMatchMedia([]);
    window.history.replaceState(null, "", "/?area=workforce&view=org&run=wfr_1");
    await render(<OrgView projectId="p1" live={new Map()} focusRunId="wfr_1" lines={() => <div />} />);
    expect($$(".ag-flow-agent[data-lit]").length).toBeLessThan(3);
    await act(async () => { await new Promise((r) => setTimeout(r, 900)); });
    expect($$(".ag-flow-agent[data-lit]")).toHaveLength(3);
    expect($$('path.ag-flow-edge[data-lit]')).toHaveLength(2);
  });

  it("a poll never moves a node: columns come from the window, not the traffic", async () => {
    stubMatchMedia([]);
    window.history.replaceState(null, "", "/?area=workforce&view=org&layout=flow");
    await render(<OrgView projectId="p1" live={new Map()} lines={() => <div />} />);
    const cols = () => $$(".ag-flow-col").map((c) => [...c.querySelectorAll<HTMLElement>(".ag-flow-node")].map((n) => n.dataset.team));
    const before = cols();
    boardWire = orgWire({ team_edges: [{ from: "media", to: "leadership", handoffs: 900, failed: 0, waiting: 0, stale: 0, last_at: null }] });
    const { notifyChanged } = await import("../app/changed");
    notifyChanged("poll");
    for (let i = 0; i < 8; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(cols()).toEqual(before);
    boardWire = orgWire();
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
