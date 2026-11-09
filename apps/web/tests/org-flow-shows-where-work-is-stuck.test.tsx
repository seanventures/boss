// @vitest-environment happy-dom
/**
 * What the independent test of Organization found (2026-10-04), each pinned as a class:
 *   D1  drawers render in <body>, where `position: fixed` is the screen (a transformed panel
 *       made the phone sheet sit 800px down);
 *   D2  a routed edge never passes through a column it skips;
 *   D3  Flow shows where work is stuck: Waiting/Failed on nodes, `You ⏸` for gates, Stuck now;
 *   D4  a pair that hands work both ways is two curves with two labels, each with an arrowhead;
 *   D5  every drawn edge says its volume (`×n`) and its opacity follows volume;
 *   D6  an ability that must be set up first links to the Kit instead of offering Give;
 *   D8  a focused run marks its failing hop;
 *   D9  the edge drawer's Activity link carries the agent and the window;
 *   D10 T keeps focus on the Map/Table toggle, so T toggles back;
 *   D11 phone controls are 44px;
 *   D14 Plan it says which planned line it replaces before it saves.
 */
import React, { act } from "react";
import { readFileSync } from "node:fs";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { stubMatchMedia } from "./_helpers/agents-home-fixtures";
import { edgeWire, orgFetch, orgWire } from "./_helpers/org-board-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { OrgView } = await import("../app/agents/org-view");
const { routeEdges } = await import("../app/agents/org-flow");
const { activityHref } = await import("../app/agents/org-edge-panel");
const { PlanVsWork } = await import("../app/agents/org-drift");
const { parseOrgBoard } = await import("../lib/agents-org-board");

let root: Root | null = null;
let host: HTMLElement | null = null;
beforeEach(() => { stubMatchMedia([]); });
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});
async function settle() {
  for (let i = 0; i < 12; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
async function mount(node: React.ReactElement, url = "/?area=workforce&view=org&layout=flow") {
  window.history.replaceState(null, "", url);
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  await settle();
}
async function render(wire: Record<string, unknown>, url?: string, extra?: Parameters<typeof orgFetch>[1]) {
  vi.stubGlobal("fetch", vi.fn(orgFetch(() => wire, extra).fn));
  await mount(<OrgView projectId="p1" live={new Map()} actor="ceo" lines={() => <div />} />, url);
}
const $ = (sel: string) => document.querySelector<HTMLElement>(sel);
const $$ = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)];
const ago = (m: number) => new Date(Date.now() - m * 60_000).toISOString();
const css = readFileSync("app/agents/agent-world.css", "utf8");

const STUCK = {
  teams: {
    quality: { waiting: [{ role_key: "qa", act_id: "w1", status: "awaiting_input", label: "Release · Ship it",
                           since: ago(19), run_id: "wfr_1", attention_id: "dec_1" }],
               waiting_total: 1, waiting_you: 1, needs_you: 1, work: { done: 3, failed: 1, open: 1 } },
  },
  failures: [{ role_key: "qa", act_id: "f1", label: "Test · Ship it", at: ago(5), run_id: "wfr_2", team: "quality" }],
};

describe("D1 drawers are on the screen", () => {
  it("the Agent panel renders in <body>, fixed to the viewport", async () => {
    await render(orgWire(), "/?area=workforce&view=org");
    await act(async () => { $('[data-testid="ag-zones-agent-qa"]')!.click(); });
    await settle();
    expect($('[data-testid="ag-org-agent"]')!.parentElement).toBe(document.body);
    expect(css).toMatch(/\.org-drawer \{\s*position: fixed; top: 64px; right: 16px; bottom: 16px;/);
    expect(css).toMatch(/\.org-drawer \{ top: auto; left: 0; right: 0; bottom: 0;/);
  });
});

describe("D2 a routed edge goes around the columns it skips", () => {
  it("an edge two columns apart runs below the middle column, never through it", () => {
    const pt = (l: number, y: number, t: number, b: number) => ({ l, r: l + 150, y, t, b });
    const points = new Map([
      ["t:a", pt(0, 40, 20, 60)], ["t:m", pt(240, 40, 20, 60)], ["t:z", pt(480, 40, 20, 60)],
      ["c:1", pt(240, 100, 0, 200)],
    ]);
    const colOf = (p: string) => ({ "t:a": 0, "t:m": 1, "t:z": 2 } as Record<string, number>)[p];
    const bottom = (from: number, to: number) => (from <= 1 && to >= 1 ? 200 : 0);
    const routes = routeEdges([{ key: "a>z", from: "a", to: "z", fromPt: "t:a", toPt: "t:z", level: "team" }],
                              points, colOf, bottom);
    const r = routes.get("team:a>z")!;
    expect(r.y).toBeGreaterThan(200);
    const ys = [...r.d.matchAll(/-?\d+(?:\.\d+)? (-?\d+(?:\.\d+)?)/g)].map((m) => Number(m[1]));
    // Anywhere over the middle column (x 240..390) the path is below it.
    const segs = r.d.split(/[MCL]/).filter(Boolean).flatMap((seg) => seg.trim().split(/\s+/))
      .reduce<number[][]>((acc, v, i, all) => (i % 2 ? acc : [...acc, [Number(v), Number(all[i + 1])]]), []);
    for (const [x, y] of segs) if (x > 240 && x < 390) expect(y).toBeGreaterThan(200);
    expect(ys.length).toBeGreaterThan(0);
  });

  it("D4 a two-way pair is two curves, offset apart, each labelled and with an arrowhead", async () => {
    await render(orgWire({ team_edges: [edgeWire("engineering", "quality", 10), edgeWire("quality", "engineering", 4)] }));
    const paths = $$('path.ag-flow-edge[data-level="team"]');
    expect(paths.map((p) => p.dataset.edge).sort()).toEqual(["engineering>quality", "quality>engineering"]);
    expect(paths[0].getAttribute("d")).not.toBe(paths[1].getAttribute("d"));
    for (const p of paths) expect(p.getAttribute("marker-end")).toMatch(/^url\(#ag-flow-arrow-/);
    const labels = $$(".ag-flow-edge-label").map((l) => l.textContent);
    expect(labels).toEqual(expect.arrayContaining(["×10", "×4"]));
  });
});

describe("D3 Flow shows where work is stuck", () => {
  it("nodes say Waiting and Failed; gates are `You ⏸`; Stuck now lists the waits and failures", async () => {
    await render(orgWire(STUCK));
    const node = $('[data-testid="ag-flow-node-quality"]')!;
    expect(node.textContent).toContain("Waiting 1, oldest 19m");
    expect(node.textContent).toContain("Failed 1");
    expect($('[data-testid="ag-flow-gate"]')!.textContent).toContain("1 waiting");
    expect($('path.ag-flow-edge[data-edge="quality>gate"]')!.dataset.gate).toBe("true");
    const stuck = $('[data-testid="ag-flow-stuck"]')!;
    expect(stuck.textContent).toContain("Stuck now");
    const wait = $('[data-testid="ag-flow-stuck-wait-w1"]')!;
    expect(wait.textContent).toContain("Needs you");
    expect(wait.querySelector("a")!.getAttribute("href")).toContain("focus=dec_1");
    const failed = $('[data-testid="ag-flow-stuck-failed-f1"]')!;
    expect(failed.textContent).toContain("Failed ✕");
    expect(failed.querySelector("a")!.getAttribute("href")).toContain("wfr=wfr_2");
  });

  it("D5 every drawn edge says its volume, and its opacity follows it", async () => {
    await render(orgWire());
    const labels = $$(".ag-flow-edge-label").map((l) => l.textContent);
    expect(labels).toEqual(expect.arrayContaining(["×31, 2 failed ✕, 1 waiting ", "×19", "×6"]));
    const op = (k: string) => Number($(`path.ag-flow-edge[data-edge="${k}"]`)!.style.strokeOpacity);
    expect(op("engineering>quality")).toBeGreaterThan(op("engineering>operations"));
  });
});

describe("D8 a focused run marks where it failed", () => {
  it("the hop into the failed agent is red and the agent says so", async () => {
    stubMatchMedia(["prefers-reduced-motion"]);
    const chain = { acts: [
      { act_id: "a0", role_key: null, kind: "run", status: "error", label: "Ship it", started_at: "2026-10-04T10:00:00Z" },
      { act_id: "a1", role_key: "software_engineer", kind: "step", status: "complete", parent_act_id: "a0", label: "Build", started_at: "2026-10-04T10:00:01Z" },
      { act_id: "a2", role_key: "qa", kind: "step", status: "error", parent_act_id: "a1", label: "Test", started_at: "2026-10-04T10:00:05Z" },
    ] };
    vi.stubGlobal("fetch", vi.fn(orgFetch(() => orgWire(), (url) => (url.includes("/chain") ? Response.json(chain) : undefined)).fn));
    await mount(<OrgView projectId="p1" live={new Map()} focusRunId="wfr_2" lines={() => <div />} />,
                "/?area=workforce&view=org&run=wfr_2");
    expect($('path.ag-flow-edge[data-edge="software_engineer>qa"]')!.dataset.state).toBe("failed");
    expect($('[data-testid="ag-flow-agent-qa"]')!.dataset.failed).toBe("true");
  });
});

describe("D9 D10 D11 links and keys", () => {
  it("the edge drawer's Activity link carries the agent and the window", () => {
    const one = new URL(activityHref(["qa"], 30), "http://x");
    expect(one.searchParams.get("view")).toBe("activity");
    expect(one.searchParams.get("agent")).toBe("qa");
    expect(one.searchParams.get("days")).toBe("7");
    expect(new URL(activityHref(["qa", "sre"], 1), "http://x").searchParams.get("agent")).toBeNull();
  });

  it("T keeps focus on the toggle, so T toggles back", async () => {
    await render(orgWire());
    const flow = $('[data-testid="ag-flow"]')!;
    $('[data-testid="ag-flow-node-engineering"]')!.focus();
    await act(async () => { document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: "t", bubbles: true })); });
    await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
    expect(flow.dataset.mode).toBe("table");
    expect(document.activeElement).toBe($('[data-testid="ag-flow-mode-table"]'));
    await act(async () => { document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: "t", bubbles: true })); });
    expect(flow.dataset.mode).toBe("map");
  });

  it("phone controls are 44px", () => {
    expect(css).toMatch(/\.ag-org-bar \.ag-seg button, \.ag-flow-bar \.ag-btn, \.ag-flow-bar \.ag-chip, \.ag-org \.ag-chip \{ min-height: 44px; \}/);
  });
});

describe("D6 an ability that must be set up first", () => {
  it("links to the Kit instead of offering Give", async () => {
    await render(orgWire({ teams: { engineering: { gaps: {
      abilities: [{ ability: "deploy", label: "Deploy", roles: ["software_engineer"], needs_setup: true }],
      step_gaps: [], missing_roles: 0, kit_ok: true, kit_reason: null } } } }), "/?area=workforce&view=org");
    await act(async () => { $('[data-testid="ag-orgteam-give-engineering"]')!.click(); });
    const link = $('[data-testid="ag-orgteam-setup-deploy-software_engineer"]')!;
    expect(link.textContent).toContain("Set up first");
    expect(link.getAttribute("href")).toContain("area=skills");
    expect(link.getAttribute("href")).toContain("ability=deploy");
    expect($('[data-testid="ag-orgteam-give-deploy-software_engineer"]')).toBeNull();
    // Nothing would be given, so Give recommended is not offered.
    await act(async () => { $('[data-testid="ag-orgteam-more-engineering"]')!.click(); });
    expect($('[data-testid="ag-orgteam-give-recommended-engineering"]')).toBeNull();
  });
});

describe("D14 Plan it says what it replaces", () => {
  it("names the line it moves before saving", async () => {
    const drift = parseOrgBoard(orgWire({ declared_vs_observed: { lines: 2, planned_unused: [],
      unplanned_pairs: [{ a: "chief_architect", b: "pm", handoffs: 9, parent: "pm", child: "chief_architect", parent_is: "sender" }] } })).drift;
    await mount(<PlanVsWork drift={drift} busy={false} onPlan={() => {}}
                            declared={{ chief_architect: { reports_to: "cto" } }} />);
    const said = $('[data-testid="org-drift-replaces-chief_architect-pm"]')!;
    expect(said.textContent).toMatch(/^Replaces .+ → .+$/);
  });
});
