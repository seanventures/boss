// @vitest-environment happy-dom
/**
 * The Agents area tells the truth (Agent World spec §2.1 one identity, §2.3 truthful edges).
 *
 * Each of these was a screen that looked right and said something false:
 *   - Replay last run keyed a workflow chain by an agent-process run id, so it never played;
 *   - a live dot ran along the agent's busiest edge, not from whoever really handed it work;
 *   - Work's links dropped `?org=`, which lands the operator on the organisation picker;
 *   - the Passport drew a stored seed-only face raw, so Vera's hero was a procedural robot,
 *     and saving any voice field wrote that seed back;
 *   - violations and the reporting line vanished from the agent page;
 *   - a Lineage edge matched node ids against role keys and read one page, so it listed nothing;
 *   - a tab switch during a poll started a second poll chain.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DETAIL, PASSPORT, installAgentWorldApi, type Call } from "./_helpers/agent-world-api";
import type { OrgChart } from "../lib/v2/workforce";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { AgentDetail } = await import("../app/agent-detail-ui");
const { AgentAvatar } = await import("../app/agent-avatar");
const { TeamSwarm } = await import("../app/agents/team-swarm");
const { PassportWork, actHref } = await import("../app/agents/passport-work");
const { PassportLineage, actsAcross } = await import("../app/agents/passport-lineage");
const { resetLiveStores, subscribeLive } = await import("../lib/agents-live");
const { identityOf, toAct, toLineage } = await import("../lib/agent-world");
const { CAST_BY_KEY } = await import("../lib/avatar-kit/cast");
const { PASSPORT_FACE_PX } = await import("../app/agents/passport-header");

let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => {
  window.history.replaceState(null, "", "/?org=acme&area=workforce&role=qa");
});
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.body.innerHTML = "";
  resetLiveStores();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 12; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
async function render(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  await settle();
}
const $ = (sel: string) => document.querySelector<HTMLElement>(sel);
const click = async (el: HTMLElement | null) => {
  expect(el).not.toBeNull();
  await act(async () => { el!.click(); });
  await settle();
};
const svgIn = (el: Element | null) => el?.querySelector(".ag-avatar-svg")?.innerHTML ?? "";

const node = (id: string, over: Record<string, unknown> = {}) => ({
  id, name: identityOf(id).name, role: id, handoffs_in: 1, handoffs_out: 1, connected: true, ...over,
});
const chartOf = (ids: string[], edges: [string, string, number][], over: Record<string, Record<string, unknown>> = {}) => ({
  project_id: "p1", window_hours: 168, apiReachable: true, nodes: ids.map((id) => node(id, over[id])),
  tree: [], roots: [], root_inferred: false, depth: 0, derived_from: "", note: "", isolated: [],
  edges: edges.map(([source, target, times]) => ({ source, target, times, last_action: null, last_at: null })),
}) as unknown as OrgChart;

describe("Team › Swarm: motion only for what really happened", () => {
  it("Replay last run plays the newest WORKFLOW run's chain, never the org chart's process run id", async () => {
    const { calls } = installAgentWorldApi([(url) => {
      if (url.startsWith("/v1/delivery-workflows/runs")) return Response.json({ runs: [{ id: "wfr_new" }, { id: "wfr_old" }] });
      if (url.startsWith("/v1/workflow-runs/wfr_new/chain")) {
        return Response.json({ acts: [
          { act_id: "a1", role_key: "pm", kind: "step", started_at: "2026-10-03T10:00:00Z" },
          { act_id: "a2", role_key: "qa", kind: "step", parent_act_id: "a1", started_at: "2026-10-03T10:00:04Z" },
        ] });
      }
      return undefined;
    }]);
    // run_id on an org node is an agent_process_runs id: it must never key a chain.
    const chart = chartOf(["pm", "qa"], [["pm", "qa", 3]], { qa: { run_id: "apr_process_1", last_activity_at: "2026-10-03T10:00:00Z" } });
    await render(<TeamSwarm projectId="p1" chart={chart} live={new Map()} />);
    await click($('[data-testid="ag-replay"]'));
    const chains = calls.filter((c: Call) => c.url.includes("/chain"));
    expect(chains.map((c) => c.url)).toEqual(["/v1/workflow-runs/wfr_new/chain?project_id=p1"]);
    expect(calls.some((c) => c.url.includes("apr_process_1"))).toBe(false);
    expect(calls.some((c) => c.url === "/v1/delivery-workflows/runs?project_id=p1&limit=5")).toBe(true);
    expect($('[data-edge="pm>qa"]')).not.toBeNull();
  });

  it("a live dot runs from the act's real sender — not the busiest edge — and nothing moves when it is unknown", async () => {
    installAgentWorldApi([(url) => {
      if (url.startsWith("/v1/acts/act_live")) {
        return Response.json({ act_id: "act_live", role_key: "qa", kind: "step", status: "active", parent_act_id: "act_parent", handed_by_kind: "boss" });
      }
      if (url.startsWith("/v1/acts/act_parent")) return Response.json({ act_id: "act_parent", role_key: "software_engineer", kind: "step" });
      if (url.startsWith("/v1/acts/")) return Response.json({ detail: "Not Found" }, { status: 404 });
      return undefined;
    }]);
    const chart = chartOf(["pm", "qa", "software_engineer", "sre"],
                          [["pm", "qa", 9], ["software_engineer", "qa", 1], ["pm", "sre", 2]]);
    await render(<TeamSwarm projectId="p1" chart={chart} live={new Map()} />);
    const live = new Map([["qa", { state: "working" as const, actId: "act_live", line: "Testing" }]]);
    await act(async () => root!.render(<TeamSwarm projectId="p1" chart={chart} live={live} />));
    await settle();
    expect([...document.querySelectorAll(".ag-swarm-dot")].map((d) => d.getAttribute("data-edge"))).toEqual(["software_engineer>qa"]);

    // sre's act has no knowable sender: no dot, even though pm→sre is drawn.
    const live2 = new Map([...live, ["sre", { state: "working" as const, actId: "act_unknown", line: "On call" }]]);
    await act(async () => root!.render(<TeamSwarm projectId="p1" chart={chart} live={live2} />));
    await settle();
    expect($('[data-edge="pm>sre"]')).toBeNull();
  });
});

describe("Work: every link keeps who you are", () => {
  it("act links and the empty state's link carry ?org=", async () => {
    const a = toAct({ act_id: "x", links: { workflow_run_id: "wfr_1" } });
    expect(actHref(a, "?org=acme&area=workforce&role=qa")).toBe("?org=acme&area=delivery_workflows&wfr=wfr_1");
    expect(actHref(toAct({ act_id: "y", code_task_run_id: "ct_1" }), "?org=acme")).toBe("?org=acme&area=code_tasks&run=ct_1");

    installAgentWorldApi([(url) => (url.includes("/acts") ? Response.json({ items: [], next: null }) : undefined)]);
    await render(<PassportWork projectId="p1" actor="ceo" roleKey="qa" name="QA Lead" />);
    const link = $('[data-testid="ag-work-empty"] a');
    expect(new URLSearchParams(link!.getAttribute("href")!).get("org")).toBe("acme");
  });

  it("the Now card's Open run keeps ?org=", async () => {
    installAgentWorldApi([(url) => (url.includes("/acts") ? Response.json({ items: [
      { act_id: "act_now", role_key: "qa", kind: "step", status: "active", workflow_run_id: "wfr_9", started_at: "2026-10-03T10:00:00Z" },
    ], next: null }) : undefined)]);
    await render(<PassportWork projectId="p1" actor="ceo" roleKey="qa" name="QA Lead"
                               presence={{ state: "working", line: "Testing", actId: "act_now" }} />);
    const open = $('[data-testid="ag-now"] a');
    expect(open?.getAttribute("href")).toBe("?org=acme&area=delivery_workflows&wfr=wfr_9");
  });
});

const seedOnly = { ...PASSPORT, persona: { ...PASSPORT.persona, avatar: { kit: "boss-v1", seed: PASSPORT.agent_id } } };

describe("the Passport face is the face everywhere", () => {
  it("qa's hero is Vera's hand-drawn face — the same markup as a chip — when the passport stores only a seed", async () => {
    installAgentWorldApi([(url) => (url.includes("/v1/agents/qa/passport") ? Response.json(seedOnly) : undefined)]);
    await render(<AgentDetail projectId="p1" roleKey="qa" actor="ceo" onClose={() => {}} />);
    const hero = svgIn($(".ag-hero-face"));
    const chip = document.createElement("div");
    chip.innerHTML = renderToStaticMarkup(<AgentAvatar agent="qa" size={PASSPORT_FACE_PX} />);
    expect(hero).not.toBe("");
    expect(hero).toBe(svgIn(chip));
  });

  it("saving a voice field never writes a face nobody picked; the face editor is the kit's picker", async () => {
    const { calls } = installAgentWorldApi([
      (url) => (url.includes("/v1/agents/qa/passport") ? Response.json(seedOnly) : undefined),
      (url, init) => (url.startsWith("/v1/agents/qa") && init.method === "PATCH"
        ? Response.json({ version: 5, fingerprint: "abcd1234", warnings: [] }) : undefined),
    ]);
    await render(<AgentDetail projectId="p1" roleKey="qa" actor="ceo" onClose={() => {}} />);
    const picker = $('[data-testid="ag-persona"] [data-testid="ag-face-picker"]');
    expect(picker).not.toBeNull();
    const hats = [...picker!.querySelectorAll<HTMLOptionElement>('[data-testid="ag-face-hat"] option')].map((o) => o.value);
    expect(hats).toContain(CAST_BY_KEY.qa.avatar.hat); // its own cast hat
    expect(hats).not.toContain(CAST_BY_KEY.engineering_captain.avatar.hat); // never another's

    const tagline = $('[data-testid="ag-tagline"]') as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(tagline, "Prove it.");
      tagline.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click($('[data-testid="ag-persona-save"]'));
    const patch = calls.filter((c) => c.method === "PATCH" && c.url === "/v1/agents/qa");
    expect(patch).toHaveLength(1);
    expect((patch[0].body!.persona as Record<string, unknown>).avatar).toBeUndefined();
  });
});

describe("the agent page keeps what the old header said", () => {
  it("shows violations and who it reports to, and the vitals from the agent page with no passport", async () => {
    installAgentWorldApi([
      (url) => (url.includes("/passport") ? Response.json({ detail: "Not Found" }, { status: 404 }) : undefined),
      (url) => (url.startsWith("/api/agents/qa") ? Response.json({
        ...DETAIL, violations: 3, reports_to: "engineering_captain",
        apps: [{ app: "billing", calls: 4, ok_calls: 4, tokens: 1, spend_usd: 2.5, stages: ["test"] }],
        panels: [{ panel_key: "prd" }],
      }) : undefined),
    ]);
    await render(<AgentDetail projectId="p1" roleKey="qa" actor="ceo" onClose={() => {}} />);
    const vitals = $('[data-testid="ag-vitals"]')?.textContent ?? "";
    for (const v of ["Trust 82", "1 app", "$2.50 spent", "1 panel"]) expect(vitals).toContain(v);
    expect($('[data-testid="ag-violations"]')?.textContent).toBe("3 violations");
    expect($('[data-testid="ag-violations"]')?.getAttribute("data-tone")).toBe("bad");
    expect($('[data-testid="ag-cert-reports"]')?.textContent).toBe("Engineering Captain");
  });

  it("with a passport, violations still show beside its vitals", async () => {
    installAgentWorldApi([(url) => (url.startsWith("/api/agents/qa") ? Response.json({ ...DETAIL, violations: 1 }) : undefined)]);
    await render(<AgentDetail projectId="p1" roleKey="qa" actor="ceo" onClose={() => {}} />);
    expect($('[data-testid="ag-vitals"]')?.textContent).toContain("41 runs");
    expect($('[data-testid="ag-violations"]')?.textContent).toBe("1 violation");
  });
});

describe("Lineage: pressing an edge lists the work that crossed it", () => {
  const graph = {
    nodes: [{ id: "agent:software_engineer", kind: "agent", role_key: "software_engineer" },
            { id: "agent:qa", kind: "agent", role_key: "qa" }, { id: "boss", kind: "boss" }],
    edges: [
      { from: "agent:software_engineer", to: "agent:qa", acts: 2, how_top: "next_stage", label: "Next stage ×2", last_at: "", reconstructed: false },
      { from: "boss", to: "agent:qa", acts: 9, how_top: "model_pick", label: "Picked by BOSS ×9", last_at: "", reconstructed: false },
    ],
  };
  const fromBoss = (n: number) => Array.from({ length: n }, (_, i) => ({ act_id: `b${i}`, role_key: "qa", kind: "step", handed_by_kind: "boss", label: `Boss ${i}` }));
  const pages = (url: string) => {
    if (!url.includes("/v1/agents/qa/acts")) return undefined;
    if (!url.includes("cursor=")) return Response.json({ items: fromBoss(12), next: "c2" });
    return Response.json({ items: [{ act_id: "s1", role_key: "qa", kind: "step", handed_by_kind: "agent", handed_by_id: "software_engineer",
                                     label: "Test · build 7", edge_label: "Next stage" }], next: null });
  };

  it("matches the other end by role key (node ids are not role keys) and pages past the first page", async () => {
    const calls: string[] = [];
    installAgentWorldApi([(url) => { calls.push(url); return pages(url); }]);
    const g = toLineage(graph);
    const acts = await actsAcross("p1", g, g.edges[0], 30);
    expect(acts.map((a) => a.actId)).toEqual(["s1"]);
    expect(calls.filter((u) => u.includes("/acts")).length).toBe(2);
  });

  it("marks the pressed edge pressed and lists its work", async () => {
    installAgentWorldApi([(url) => (url.includes("/lineage") ? Response.json(graph) : pages(url))]);
    await render(<PassportLineage projectId="p1" actor="ceo" roleKey="qa" />);
    const edge = $('[data-testid="ag-ln-edge-agent:software_engineer-agent:qa"]');
    await click(edge);
    expect(edge?.getAttribute("aria-pressed")).toBe("true");
    expect($('[data-testid="ag-ln-acts"]')?.textContent).toContain("Test · build 7");
    expect($('[data-testid="ag-ln-acts"]')?.textContent).not.toContain("None in this window");
  });
});

describe("presence: one shared poll, whatever the tab does", () => {
  it("coming back to the tab during a pending poll leaves exactly one poll chain", async () => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    let release: (r: Response) => void = () => {};
    let n = 0;
    vi.stubGlobal("fetch", vi.fn(() => {
      n += 1;
      if (n === 1) return new Promise<Response>((r) => { release = r; });
      return Promise.resolve(Response.json({ items: [] }));
    }));
    const stop = subscribeLive("p_poll", () => {});
    await vi.advanceTimersByTimeAsync(0);
    expect(n).toBe(1); // first poll in flight
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    release(Response.json({ items: [] }));
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(1);
    for (let i = 0; i < 3; i += 1) await vi.advanceTimersByTimeAsync(4000);
    expect(n).toBe(4); // one fetch per 4s, not two
    expect(vi.getTimerCount()).toBe(1);
    stop();
  });
});
