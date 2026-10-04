// @vitest-environment happy-dom
/**
 * Tooltips, not paragraphs — on every Agent World surface this stream owns (spec §2.7, §9, §12.4
 * test 14): the Passport (header, sections nav, Work, Voice & Face, Test, Lineage), the share card,
 * the Roster zones and the crew, Team › Swarm, and New agent (Face & voice, From archetype, the
 * birth). Each is RENDERED with fixtures and its visible text walked with the Studio's walker:
 * no run over six words outside a tooltip, no <p> inside an `ag-` surface, and none of the
 * banned words (persona, fingerprint, ledger, act, sentient, alive, conscious, impersonate,
 * signed, token) — with the spec's own labels `No signed work yet`, `Local · tokens coming` and
 * the `Lineage` section name excepted. Text an operator or the API wrote is `data-user-text`.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { installAgentWorldApi } from "./_helpers/agent-world-api";
import type { OrgChart } from "../lib/v2/workforce";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { AgentDetail } = await import("../app/agent-detail-ui");
const { AgentQuickCreate } = await import("../app/agent-quick-create");
const { RosterZones } = await import("../app/agents/roster-zones");
const { PresencePill } = await import("../app/agents/presence-pill");
const { TeamSwarm } = await import("../app/agents/team-swarm");
const { AgentBirth } = await import("../app/agents/agent-birth");
const { ShareCardDialog } = await import("../app/agents/share-card-dialog");
const { parseLive, resetLiveStores } = await import("../lib/agents-live");
const { LiveBoardView } = await import("../app/agents/live-board");
const { parseLiveBoard } = await import("../lib/agents-live-board");
const fixture = await import("./_helpers/live-board-fixture");
const { identityOf } = await import("../lib/agent-world");

const SURFACES = [".ag-passport", ".ag-pnav", ".ag-work", ".ag-persona", ".ag-test", ".ag-lineage", ".ag-share",
                  ".ag-roster", ".ag-roster-sum", ".ag-pill-wrap", ".ag-crew", ".ag-team", ".ag-fv",
                  ".ag-archetypes", ".ag-birth",
                  // Agents home (agents-home spec §5.1): Live, its cards and its run rows.
                  ".ag-live", ".ag-att", ".ag-mission"];
const SKIP = "[data-tip], [role='tooltip'], [title], textarea, script, style, option, [hidden], "
  + "[aria-hidden='true'], .ag-sr, [data-user-text]";
const BANNED = /\b(persona|fingerprint|ledger|act|acts|sentient|alive|conscious|impersonate|signed|tokens?|eta|estimated|missions?|office)\b/i;
const ALLOWED = ["No signed work yet", "Local · tokens coming", "Lineage"];

function visibleRuns(root: Element): string[] {
  const out: string[] = [];
  for (const el of [root, ...root.querySelectorAll("*")]) {
    if (el.closest(SKIP)) continue;
    const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent ?? "").join("").trim();
    if (own) out.push(own);
  }
  return out;
}

/** Every rule, on every ag- surface on the page. Returns what broke, so a failure names it. */
function audit(): { surfaces: number; over: string[]; paragraphs: string[]; banned: string[] } {
  const roots = SURFACES.flatMap((s) => [...document.querySelectorAll(s)]);
  const over = roots.flatMap((r) => overBudget(r));
  const paragraphs = roots.flatMap((r) => [...r.querySelectorAll("p")].map((p) => p.textContent ?? ""));
  const banned = roots.flatMap(visibleRuns).filter((t) => !ALLOWED.some((a) => t === a || t.includes(a)) && BANNED.test(t));
  return { surfaces: roots.length, over: [...new Set(over)], paragraphs, banned: [...new Set(banned)] };
}

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.body.innerHTML = "";
  resetLiveStores();
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
  // Open everything that opens, so hidden copy is counted too.
  await act(async () => {
    document.querySelectorAll("details").forEach((d) => { d.open = true; });
    document.querySelectorAll<HTMLElement>(".ag-act-main, [data-testid='ag-more']").forEach((b) => b.click());
  });
  await settle();
}
const $ = (sel: string) => document.querySelector<HTMLElement>(sel);

describe("the Agent World copy budget", () => {
  it("the Passport: header, menu, sections, Work, Voice & Face, Test, Lineage", async () => {
    installAgentWorldApi();
    window.history.replaceState(null, "", "/?area=workforce&role=qa");
    await render(<AgentDetail projectId="p1" roleKey="qa" actor="ceo" onClose={() => {}} />);
    await act(async () => { $('[data-testid="ag-ln-edge-boss-qa"]')!.click(); });
    await settle();
    const r = audit();
    expect(SURFACES.filter((s) => document.querySelector(s))).toEqual([".ag-passport", ".ag-pnav", ".ag-work", ".ag-persona", ".ag-test", ".ag-lineage"]);
    expect(r.surfaces).toBeGreaterThanOrEqual(6);
    expect(r.over).toEqual([]);
    expect(r.paragraphs).toEqual([]);
    expect(r.banned).toEqual([]);
  });

  it("the share card", async () => {
    installAgentWorldApi();
    await render(<ShareCardDialog projectId="p1" roleKey="qa" name="Vera" version={4} onClose={() => {}} />);
    const r = audit();
    expect(r.surfaces).toBe(1);
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
  });

  it("the Roster zones, the summary, the pill and the crew", async () => {
    installAgentWorldApi();
    const live = parseLive([{ role_key: "qa", state: "needs_you", line: "approve deploy to production now please" },
                            { role_key: "pm", state: "working", line: "drafting the PRD" }]);
    const agents = ["qa", "pm", "docs_writer"].map((id) => ({
      id, name: identityOf(id).name, role: id, status: "IDLE", task: "No work in this window.", model: null,
      tokens: null, spend_usd: null, calls: 3, autonomy_level: 1, trust_score: 80, successful_runs: 12,
      violations: 0, hidden_failures: 0, last_seen_seconds: null, run_id: null, project: null, last_touched: null,
      registered: id !== "docs_writer",
    })) as never[];
    await render(<div>
      <RosterZones agents={agents} live={live} onOpen={() => {}} onAsk={() => {}} onRegister={() => {}} />
      <PresencePill projectId="p1" live={live} />
    </div>);
    await act(async () => { $('[data-testid="ag-presence-pill"]')!.click(); });
    const r = audit();
    expect(r.surfaces).toBeGreaterThanOrEqual(3);
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
  });

  it("Agents › Live: cards (Details open), run rows, outcomes, every empty state", async () => {
    installAgentWorldApi();
    const noop = () => {};
    const live = (body: Record<string, unknown>) => ({ board: parseLiveBoard(body), error: null, loading: false, refresh: noop });
    const draw = (body: Record<string, unknown>) => (
      <LiveBoardView projectId="p1" actor="ceo" live={live(body)} onOpenMap={noop} onActivity={noop}
                     onDirectory={noop} onNewAgent={noop} paused />);
    await render(draw(fixture.board({ attention: [fixture.gate(), fixture.failed(), fixture.prReview()],
      missions: [fixture.mission(), fixture.mission({ run_id: "w2", state: "waiting", waiting_on: "build to finish" }),
                 fixture.mission({ run_id: "w3", state: "done", elapsed_seconds: 2520 }),
                 fixture.mission({ run_id: "w4", state: "blocked", blocked_reason: "no credit" })] })));
    await act(async () => { document.querySelectorAll<HTMLElement>('[data-testid="ag-att-details"]').forEach((b) => b.click()); });
    await act(async () => { document.querySelector<HTMLElement>('[data-testid="ag-att-reject"]')?.click(); });
    let r = audit();
    expect(r.surfaces).toBeGreaterThanOrEqual(5);
    // No percent next to a run (spec §5.1): nothing on Live can honestly compute one.
    expect(visibleRuns(document.querySelector(".ag-live")!).filter((t) => t.includes("%"))).toEqual([]);
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
    act(() => root?.unmount());
    await render(draw(fixture.board({ attention: [], missions: [], outcomes: [] })));
    r = audit();
    expect(r.surfaces).toBe(1);
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
  });

  it("Team › Swarm", async () => {
    installAgentWorldApi();
    const node = (id: string) => ({ id, name: identityOf(id).name, role: id, handoffs_in: 1, handoffs_out: 1, connected: true });
    const chart = { project_id: "p1", window_hours: 168, apiReachable: true, nodes: [node("pm"), node("qa"), node("software_engineer")],
      tree: [], roots: [], root_inferred: false, depth: 0, derived_from: "", note: "", isolated: [],
      edges: [{ source: "pm", target: "software_engineer", times: 4, last_action: null, last_at: null },
               { source: "software_engineer", target: "qa", times: 2, last_action: null, last_at: null }] } as unknown as OrgChart;
    await render(<TeamSwarm projectId="p1" chart={chart} live={new Map()} />);
    expect(document.querySelectorAll(".ag-swarm-node")).toHaveLength(3);
    const r = audit();
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
  });

  it("New agent: Face & voice, From archetype with one picked, and the birth", async () => {
    installAgentWorldApi();
    await render(<AgentQuickCreate projectId="p1" actor="ceo" onClose={() => {}} onCreated={() => {}} />);
    let r = audit();
    expect(r.surfaces).toBe(1);
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
    await act(async () => { $('[data-testid="aq-tab-archetype"]')!.click(); });
    await act(async () => { $('[data-testid="aq-arch-teacher_who_simplifies"]')!.click(); });
    r = audit();
    expect(r.surfaces).toBe(1);
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
    act(() => root?.unmount());
    await render(<AgentBirth identity={identityOf("qa")} agentId="agt_7f3k2q4m9x" onDone={() => {}} />);
    r = audit();
    expect(r.surfaces).toBe(1);
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
  });
});
