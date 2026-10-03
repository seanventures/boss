// @vitest-environment happy-dom
/**
 * The roster is grouped by what each agent is doing, and the top bar says who is working
 * (Agent World spec §3.2, §3.3, §0.2 C — S-agents acceptance "the Roster zone equals presence").
 *
 * Presence comes from /v1/agents/live (open acts on the server). The roster, the pill and the
 * crew read ONE store, so they can never disagree. Rendered and asserted on what is drawn:
 * NEEDS YOU first, empty zones hidden, idle past twelve folds to +n, a card moves zone when its
 * presence changes, the pill hides when nothing is live, and pressing an agent in the crew opens
 * a conversation with THAT agent.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { V2Agent, Workforce } from "../lib/v2/workforce";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
vi.mock("../app/focused-agents-ui", () => ({ FocusedAgents: () => <div data-testid="focused-agents-stub" /> }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { WorkforceArea } = await import("../app/workforce-ui");
const { RosterZones, presenceFromStatus } = await import("../app/agents/roster-zones");
const { PresencePill, crewOf, pillWords } = await import("../app/agents/presence-pill");
const { parseLive, resetLiveStores } = await import("../lib/agents-live");

const agent = (id: string, over: Partial<V2Agent> = {}): V2Agent => ({
  id, name: id.replace(/_/g, " "), role: id, status: "IDLE", task: "No work in this window.", model: null,
  tokens: null, spend_usd: null, calls: 0, autonomy_level: 1, trust_score: 50, successful_runs: 0, violations: 0,
  hidden_failures: 0, last_seen_seconds: null, run_id: null, project: null, last_touched: null, registered: true,
  ...over,
} as V2Agent);

let root: Root | null = null;
let host: HTMLElement | null = null;
let live: unknown[] = [];

beforeEach(() => {
  window.history.replaceState(null, "", "/?area=workforce");
  document.title = "BOSS";
  live = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const url = String(input);
    if (url.includes("/v1/agents/live")) return Response.json({ items: live });
    return Response.json({});
  }));
});
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.body.innerHTML = "";
  resetLiveStores();
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 10; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
async function render(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  await settle();
}
const $ = (sel: string) => document.querySelector<HTMLElement>(sel);
const zones = () => [...document.querySelectorAll<HTMLElement>("[data-zone]")]
  .filter((el) => el.tagName === "SECTION").map((el) => el.dataset.zone);
const zoneOfCard = (id: string) => $(`[data-testid="wf-agent-${id}"]`)?.dataset.zone;

const workforce = (agents: V2Agent[]): Workforce => ({
  project_id: "p1", window_hours: 24, apiReachable: true, handoffs: [], agents, roster: agents,
  counts: { WORKING: 0, STUCK: 0, IDLE: agents.length, total: agents.length },
});

describe("the roster zones are presence", () => {
  it("puts each agent in the zone its live presence says, NEEDS YOU first, empty zones hidden", async () => {
    live = [
      { role_key: "qa", state: "needs_you", act_id: "act_9", line: "approve deploy" },
      { role_key: "software_engineer", state: "working", line: "running tests · Ship it · step 4" },
      { role_key: "design_critic", state: "in_panel", line: "PRD review" },
    ];
    await render(<WorkforceArea workforce={workforce([agent("qa"), agent("software_engineer"), agent("design_critic"),
                                                      agent("docs_writer")])} actor="ceo" />);
    expect(zones()).toEqual(["needs_you", "working", "in_panel", "idle"]);
    expect(zoneOfCard("qa")).toBe("needs_you");
    expect(zoneOfCard("software_engineer")).toBe("working");
    expect(zoneOfCard("design_critic")).toBe("in_panel");
    expect(zoneOfCard("docs_writer")).toBe("idle");
    expect($('[data-testid="ag-line-qa"]')?.textContent).toContain("Waiting on you: approve deploy");
    expect($('[data-testid="ag-line-software_engineer"]')?.textContent).toContain("Now: running tests · Ship it · step 4");
    expect($('[data-testid="ag-roster-sum"]')?.textContent).toContain("4 agents · 2 working");
  });

  it("with no live answer, falls back to the feed's status — the same zone either way", async () => {
    await render(<WorkforceArea workforce={workforce([agent("qa", { status: "AT GATE", task: "approve deploy" }),
                                                      agent("cto", { status: "WORKING", task: "review" }),
                                                      agent("pm")])} actor="ceo" />);
    expect(zones()).toEqual(["needs_you", "working", "idle"]);
    expect(presenceFromStatus(agent("x", { status: "STUCK", task: "build failed" })).state).toBe("stuck");
  });

  it("a card moves to its new zone when presence changes", async () => {
    const agents = [agent("qa"), agent("pm")];
    const draw = (l: Map<string, { state: "working" | "idle"; line: string }>) =>
      <RosterZones agents={agents} live={l} onOpen={() => {}} onAsk={() => {}} />;
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root!.render(draw(new Map())));
    expect(zoneOfCard("qa")).toBe("idle");
    await act(async () => root!.render(draw(new Map([["qa", { state: "working", line: "testing" }]]))));
    expect(zoneOfCard("qa")).toBe("working");
    expect(zones()).toEqual(["working", "idle"]);
  });

  it("idle past twelve folds into +n, which opens the rest", async () => {
    const many = Array.from({ length: 15 }, (_, i) => agent(`role_${i}`));
    await render(<RosterZones agents={many} live={new Map()} onOpen={() => {}} onAsk={() => {}} />);
    expect(document.querySelectorAll('[data-zone="idle"] article')).toHaveLength(12);
    const more = $('[data-testid="ag-idle-more"]');
    expect(more?.textContent).toBe("+3");
    await act(async () => { more!.click(); });
    expect(document.querySelectorAll('[data-zone="idle"] article')).toHaveLength(15);
  });

  it("Needs you is a filter, counted from the same presence", async () => {
    live = [{ role_key: "qa", state: "needs_you", line: "approve" }];
    await render(<WorkforceArea workforce={workforce([agent("qa"), agent("pm")])} actor="ceo" />);
    const chip = $('[data-testid="wf-filter-needs_you"]');
    expect(chip?.textContent).toBe("Needs you 1");
    await act(async () => { chip!.click(); });
    await settle();
    expect($('[data-testid="wf-agent-qa"]')).not.toBeNull();
    expect($('[data-testid="wf-agent-pm"]')).toBeNull();
  });
});

describe("the presence pill and the crew", () => {
  it("is hidden when nothing is live", async () => {
    await render(<PresencePill projectId="p1" live={new Map()} />);
    expect($('[data-testid="ag-presence-pill"]')).toBeNull();
  });

  it("says who is working and who needs you, and prefixes the tab title", async () => {
    const l = parseLive({ items: [
      { role_key: "qa", state: "needs_you", line: "approve deploy" },
      { role_key: "software_engineer", state: "working", line: "build" },
      { role_key: "release_manager", state: "working", line: "checklist" },
      { role_key: "pm", state: "idle", line: "Last: PRD" },
    ] });
    expect(pillWords(crewOf(l))).toBe("2 working · 1 needs you");
    await render(<PresencePill projectId="p1" live={l} />);
    const pill = $('[data-testid="ag-presence-pill"]');
    expect(pill?.textContent).toContain("2 working · 1 needs you");
    expect(pill?.getAttribute("title")).toBe("Agents working now");
    expect(document.title).toBe("(1) BOSS");
  });

  it("the crew lists who is on what, needs-you first, and opens a talk with the one pressed", async () => {
    const l = parseLive([
      { role_key: "software_engineer", state: "working", line: "build" },
      { role_key: "qa", state: "needs_you", line: "approve deploy" },
    ]);
    await render(<PresencePill projectId="p1" live={l} />);
    await act(async () => { $('[data-testid="ag-presence-pill"]')!.click(); });
    const rows = [...document.querySelectorAll<HTMLElement>('[data-testid^="ag-crew-"]')]
      .map((r) => r.dataset.testid).filter((t) => t !== "ag-crew-roster");
    expect(rows).toEqual(["ag-crew-qa", "ag-crew-software_engineer"]);
    await act(async () => { $('[data-testid="ag-crew-qa"]')!.click(); });
    // Until S-converse lands, the talk is the side dock addressed to that agent.
    const q = new URLSearchParams(window.location.search);
    expect(q.get("dock_role")).toBe("QA");
    expect($('[data-testid="ag-crew"]')).toBeNull();
  });

  it("polls /v1/agents/live and draws what it answers", async () => {
    live = [{ role_key: "qa", state: "working", line: "tests" }];
    await render(<PresencePill projectId="p1" />);
    expect($('[data-testid="ag-presence-pill"]')?.textContent).toContain("1 working");
    expect((fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls
      .some((c) => String(c[0]).startsWith("/v1/agents/live?project_id=p1"))).toBe(true);
  });
});
