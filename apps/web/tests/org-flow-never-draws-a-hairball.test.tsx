// @vitest-environment happy-dom
/**
 * Flow never draws a hairball (org-view spec §4.3): at most 12 team edges, the rest counted into
 * the Table; isolating a team dims everything that is not its neighbour and opens it to its
 * agents with agent-level edges (only then); the Table lists every edge, sorted; and a failed
 * edge SAYS failed — a word and a glyph, not only a class. An edge opens its evidence.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { stubMatchMedia } from "./_helpers/agents-home-fixtures";
import { edgeWire, orgFetch, orgWire, teamWire } from "./_helpers/org-board-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { OrgView } = await import("../app/agents/org-view");

let root: Root | null = null;
let host: HTMLElement | null = null;
let stub: ReturnType<typeof orgFetch>;
beforeEach(() => { stubMatchMedia([]); window.history.replaceState(null, "", "/?area=workforce&view=org&layout=flow"); });
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
async function render(wire: Record<string, unknown>, extra?: Parameters<typeof orgFetch>[1]) {
  stub = orgFetch(() => wire, extra);
  vi.stubGlobal("fetch", vi.fn(stub.fn));
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<OrgView projectId="p1" live={new Map()} actor="ceo" lines={() => <div />} />));
  await settle();
}
const $ = (sel: string) => document.querySelector<HTMLElement>(sel);
const $$ = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)];

/** 40 team edges among 10 custom teams. */
function hairball() {
  const keys = Array.from({ length: 10 }, (_, i) => `team${i}`);
  const teams = keys.map((k, i) => teamWire(k, { label: `Team ${i}`, members: [`agent${i}`], flow_col: i % 4 }));
  const edges = [];
  let n = 100;
  for (const a of keys) for (const b of keys) if (a !== b && edges.length < 40) edges.push(edgeWire(a, b, n--));
  return { ...orgWire({ team_edges: edges, agent_edges: [] }), teams };
}

describe("Flow", () => {
  it("draws at most 12 team edges and counts the rest into the Table", async () => {
    await render(hairball());
    expect($('[data-testid="ag-flow"]')).not.toBeNull();
    expect($$("path.ag-flow-edge")).toHaveLength(12);
    expect($('[data-testid="ag-flow-hidden"]')!.textContent).toBe("+28 smaller handoffs in Table");
    // The Table is the accessible Flow: always in the DOM, every edge, sorted by handoffs.
    const rows = $$('[data-testid="ag-flow-table"] tbody tr');
    expect(rows).toHaveLength(40);
    const counts = rows.map((r) => Number(r.children[2].textContent));
    expect(counts).toEqual([...counts].sort((a, b) => b - a));
  });

  it("a failed edge says failed in words, and its node says it too", async () => {
    await render(orgWire());
    const state = $$(".ag-flow-edge-state").find((b) => b.dataset.state === "failed")!;
    expect(state.textContent).toBe("2 failed ✕ · 1 waiting ⏸");
    expect($('path.ag-flow-edge[data-edge="engineering>quality"]')!.dataset.state).toBe("failed");
    expect($('[data-testid="ag-flow-node-engineering"]')!.getAttribute("aria-label"))
      .toBe("Engineering, 4 agents, hands to Quality 31 times, 2 failed");
    const row = $('[data-testid="ag-flow-row-engineering-quality"]')!;
    expect(row.textContent).toContain("2 failed ✕");
  });

  it("isolating a team dims non-neighbours and opens it to agent-level edges — only then", async () => {
    await render(orgWire());
    expect($$('path.ag-flow-edge[data-level="agent"]')).toHaveLength(0);
    await act(async () => { $('[data-testid="ag-flow-node-engineering"]')!.click(); });
    await settle();
    expect(new URL(window.location.href).searchParams.get("isolate")).toBe("engineering");
    expect($('[data-testid="ag-flow-isolate"]')!.textContent).toContain("Engineering");
    const dim = (k: string) => $(`.ag-flow-node[data-team="${k}"]`)!.dataset.dim;
    expect(dim("engineering")).toBeUndefined();
    expect(dim("quality")).toBeUndefined();
    expect(dim("leadership")).toBeUndefined();
    expect(dim("media")).toBe("isolate");
    expect($$(".ag-flow-agent").map((a) => a.dataset.role).sort()).toEqual(
      ["backend_engineer", "engineering_captain", "frontend_engineer", "software_engineer"]);
    const agentEdges = $$('path.ag-flow-edge[data-level="agent"]').map((p) => p.dataset.edge).sort();
    expect(agentEdges).toEqual(["backend_engineer>adversarial_reviewer", "pm>software_engineer",
                                "software_engineer>qa", "software_engineer>sre"]);
    // Esc clears the isolate.
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); });
    expect($('[data-testid="ag-flow-isolate"]')).toBeNull();
  });

  it("pressing an edge opens its evidence: the pairs and the last handoffs", async () => {
    await render(orgWire(), (url) => (url.startsWith("/v1/agents/org-board/handoffs") ? Response.json({ items: [
      { act_id: "h1", from: "software_engineer", to: "qa", status: "error", label: "Test · Ship it #182",
        how: "Picked by BOSS", started_at: new Date().toISOString(), run_id: "wfr_2" }] }) : undefined));
    await act(async () => { $$(".ag-flow-edge-state")[0].click(); });
    await settle();
    const panel = $('[data-testid="ag-org-edge"]')!;
    expect(panel.textContent).toContain("Engineering → Quality");
    expect($('[data-testid="ag-org-edge-summary"]')!.textContent).toBe("31 handoffs · 2 failed · 1 waiting");
    expect($('[data-testid="ag-org-edge-pair-software_engineer-qa"]')!.textContent).toContain("×18");
    const rows = $('[data-testid="ag-org-edge-rows"]')!;
    expect(rows.textContent).toContain("Failed");
    expect(rows.querySelector("a")!.getAttribute("href")).toContain("wfr=wfr_2");
    const call = stub.calls.find((c) => c.url.startsWith("/v1/agents/org-board/handoffs"))!;
    expect(call.url).toContain("source=engineering_captain%2Csoftware_engineer%2Cbackend_engineer%2Cfrontend_engineer");
  });

  it("T toggles Map and Table; under 640px the Table is Flow", async () => {
    await render(orgWire());
    expect($('[data-testid="ag-flow"]')!.dataset.mode).toBe("map");
    await act(async () => { $('[data-testid="ag-flow"]')!.dispatchEvent(new KeyboardEvent("keydown", { key: "t", bubbles: true })); });
    expect($('[data-testid="ag-flow"]')!.dataset.mode).toBe("table");
    act(() => root?.unmount());
    host?.remove();
    stubMatchMedia(["max-width: 639px"]);
    await render(orgWire());
    expect($('[data-testid="ag-flow"]')!.dataset.mode).toBe("table");
    expect($(".ag-flow-stage")).toBeNull();
  });

  it("no handoffs in the window says so and offers 30d", async () => {
    await render(orgWire({ team_edges: [], agent_edges: [] }));
    expect($('[data-testid="ag-flow-empty"]')!.textContent).toContain("No handoffs in 7d");
    await act(async () => { $('[data-testid="ag-flow-widen"]')!.click(); });
    expect(new URL(window.location.href).searchParams.get("days")).toBe("30");
  });
});
