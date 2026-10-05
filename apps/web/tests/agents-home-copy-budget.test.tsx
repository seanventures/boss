// @vitest-environment happy-dom
/**
 * Tooltips, not paragraphs — on the Agents home surfaces this stream owns (agents-home spec
 * §5.1): Live › Agent force, Organization (Teams, Flow, the panels, Plan vs work, the list),
 * Directory and Activity. Each is
 * RENDERED with fixtures, opened, and walked with the Studio's walker: no run over six words
 * outside a tooltip, no <p>, and none of the Agent World banned words plus this spec's own
 * (`%`, ETA, estimated, mission, office, alive) and Organization's (utilisation, capacity,
 * productivity, score, rank — org-view spec §4.9). Text an operator or the API wrote is
 * `data-user-text`.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { board, stubMatchMedia } from "./_helpers/agents-home-fixtures";
import { orgFetch, orgWire } from "./_helpers/org-board-fixtures";
import type { V2Agent } from "../lib/v2/workforce";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
vi.mock("../app/focused-agents-ui", () => ({ FocusedAgents: () => <div /> }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { TeamZones } = await import("../app/agents/team-zones");
const { OrgView } = await import("../app/agents/org-view");
const { ActivityRiver } = await import("../app/agents/activity-river");
const { DirectoryView } = await import("../app/agents/directory-view");

const SURFACES = [".ag-team-force", ".ag-zones", ".ag-org", ".ag-orgteam", ".ag-flow", ".org-drift",
                  "[data-testid='ag-org-agent']", "[data-testid='ag-org-edge']", ".ag-river", ".ag-dir", ".ag-dir-view"];
const SKIP = "[data-tip], [role='tooltip'], [title], textarea, script, style, option, [hidden], "
  + "[aria-hidden='true'], .ag-sr, [data-user-text]";
const BANNED = /(\b(persona|fingerprint|ledger|act|acts|sentient|alive|conscious|impersonate|signed|tokens?|eta|estimated|mission|office|utilisation|utilization|capacity|productivity|score|rank)\b|%)/i;

function visibleRuns(root: Element): string[] {
  const out: string[] = [];
  for (const el of [root, ...root.querySelectorAll("*")]) {
    if (el.closest(SKIP)) continue;
    const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent ?? "").join("").trim();
    if (own) out.push(own);
  }
  return out;
}
function audit() {
  const roots = SURFACES.flatMap((s) => [...document.querySelectorAll(s)]);
  return {
    surfaces: roots.length,
    over: [...new Set(roots.flatMap((r) => overBudget(r)))],
    paragraphs: roots.flatMap((r) => [...r.querySelectorAll("p")].map((p) => p.textContent ?? "")),
    banned: [...new Set(roots.flatMap(visibleRuns).filter((t) => BANNED.test(t)))],
  };
}

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.body.innerHTML = "";
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
const clickAll = async (sel: string) => {
  for (const b of [...document.querySelectorAll<HTMLElement>(sel)]) await act(async () => { b.click(); });
  await settle();
};
const live = { software_engineer: ["working", "Now: Coding · Build · Ship it #182 and a long line"],
               qa: ["needs_you", "Waiting on you: Publish release video"] } as Record<string, [string, string]>;

describe("the Agents home copy budget", () => {
  it("Live › Agent force, every team opened", async () => {
    stubMatchMedia([]);
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({})));
    await render(<TeamZones projectId="p1" board={board(live)} density="compact" onOpenMap={() => {}} />);
    await clickAll(".ag-team-pill");
    const r = audit();
    expect(r.surfaces).toBe(1);
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
  });

  const RICH = {
    teams: {
      engineering: {
        work: { done: 23, failed: 2, open: 3 },
        waiting: [{ role_key: "software_engineer", act_id: "a1", status: "awaiting_input", label: "Build · Ship it #182 with a long label",
                    since: new Date(Date.now() - 72 * 60_000).toISOString(), run_id: "wfr_1", attention_id: "dec_1" }], waiting_total: 3,
        members_load: [{ role_key: "software_engineer", busy_seconds: 32400 }, { role_key: "backend_engineer", busy_seconds: 600 },
                       { role_key: "frontend_engineer", busy_seconds: 0 }, { role_key: "engineering_captain", busy_seconds: 0 }],
        spend: { metered_usd: 4.1, plan_calls: 120 }, models: [{ model: "claude-sonnet-4-6", calls: 88 }, { model: "haiku", calls: 32 }],
        self_handoffs: 14,
        gaps: { abilities: [{ ability: "tests_ci", label: "Tests & CI", roles: ["backend_engineer"] },
                            { ability: "open_prs", label: "Pull requests", roles: ["software_engineer"] }],
                step_gaps: [{ workflow_key: "ship_it", workflow_name: "Ship it", step_id: "build", step_title: "Build",
                              role_key: "software_engineer", ability: "deploy", label: "Deploy" }],
                missing_roles: 1, kit_ok: true, kit_reason: null },
      },
      media: { gaps: { abilities: [], step_gaps: [], missing_roles: 0, kit_ok: false, kit_reason: "kit file unreadable" } },
    },
    agents: {
      software_engineer: {
        work: { done: 14, failed: 1, open: 1 }, busy_seconds: 32400, spend: { metered_usd: 2.1, plan_calls: 40 },
        models: [{ model: "claude-sonnet-4-6", calls: 40 }],
        open: [{ role_key: "software_engineer", act_id: "a1", status: "awaiting_input", label: "Build · Ship it #182 with a long label",
                 since: new Date(Date.now() - 72 * 60_000).toISOString(), run_id: "wfr_1", attention_id: "dec_1" }],
        named_on: [{ workflow_key: "ship_it", workflow_name: "Ship it", step_id: "build", step_title: "Build the thing" }],
        gaps: { abilities: [{ ability: "open_prs", label: "Pull requests" }],
                step_gaps: [{ workflow_key: "ship_it", workflow_name: "Ship it", step_id: "build", step_title: "Build",
                              role_key: "software_engineer", ability: "deploy", label: "Deploy" }] },
      },
    },
    missing_roles: [{ role_key: "data_engineer", team: "engineering",
                      steps: [{ workflow_key: "ship_it", workflow_name: "Ship it", step_id: "load", step_title: "Load" }] }],
  };
  const liveMap = new Map([["software_engineer", { state: "working" as const, line: live.software_engineer[1] }],
                           ["qa", { state: "needs_you" as const, line: live.qa[1] }]]);

  it("Organization › Teams, a card's menus and the Agent panel open", async () => {
    stubMatchMedia(["min-width: 1024px"]);
    window.history.replaceState(null, "", "/?area=workforce&view=org");
    vi.stubGlobal("fetch", vi.fn(orgFetch(() => orgWire(RICH)).fn));
    await render(<OrgView projectId="p1" live={liveMap} actor="ceo" lines={() => <div />} />);
    await clickAll('[data-testid="ag-orgteam-more-waiting-engineering"]');
    await clickAll('[data-testid="ag-orgteam-more-engineering"]');
    let r = audit();
    expect(r.surfaces).toBeGreaterThanOrEqual(3);
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
    await clickAll('[data-testid="ag-zones-agent-software_engineer"]');
    await clickAll('[data-testid="ag-zones-more"]');
    r = audit();
    expect(document.querySelector("[data-testid='ag-org-agent']")).not.toBeNull();
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
  });

  it("Organization › Flow: the map, an isolate, an edge drawer, the Table", async () => {
    stubMatchMedia([]);
    window.history.replaceState(null, "", "/?area=workforce&view=org&layout=flow");
    vi.stubGlobal("fetch", vi.fn(orgFetch(() => orgWire(RICH), (url) => (url.startsWith("/v1/agents/org-board/handoffs")
      ? Response.json({ items: [{ act_id: "h1", from: "software_engineer", to: "qa", status: "error",
                                  label: "Test · Ship it #182 with a long label", how: "Picked by BOSS",
                                  started_at: new Date().toISOString(), run_id: "wfr_2" }] }) : undefined)).fn));
    await render(<OrgView projectId="p1" live={liveMap} actor="ceo" lines={() => <div />} />);
    await clickAll('[data-testid="ag-flow-node-engineering"]');
    await clickAll(".ag-flow-edge-state");
    let r = audit();
    expect(document.querySelector("[data-testid='ag-org-edge']")).not.toBeNull();
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
    await clickAll('[data-testid="ag-flow-mode-table"]');
    await clickAll(".ag-flow-table [aria-expanded='false']");
    r = audit();
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
  });

  it("Organization › Lines: Plan vs work", async () => {
    const { OrgChartArea } = await import("../app/workforce-ui");
    const { parseOrgBoard } = await import("../lib/agents-org-board");
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({})));
    const drift = parseOrgBoard(orgWire({ declared_vs_observed: { lines: 2,
      planned_unused: [{ agent: "adversarial_reviewer", reports_to: "engineering_captain" }],
      unplanned_pairs: [{ a: "software_engineer", b: "adversarial_reviewer", handoffs: 31, parent: "software_engineer",
                          child: "adversarial_reviewer", parent_is: "sender" }] } })).drift;
    const chart = { project_id: "p1", window_hours: 168, apiReachable: true, derived_from: "", note: "", root_inferred: false,
                    nodes: [], tree: [], roots: [], declared: { adversarial_reviewer: { reports_to: "engineering_captain" } } };
    await render(<OrgChartArea projectId="p1" actor="ceo" chart={chart as never} drift={drift} />);
    const r = audit();
    expect(r.surfaces).toBe(1);
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
  });

  it("Organization under 640px: the list, opened, and Flow as the Table", async () => {
    stubMatchMedia(["max-width: 639px"]);
    window.history.replaceState(null, "", "/?area=workforce&view=org");
    vi.stubGlobal("fetch", vi.fn(orgFetch(() => orgWire(RICH)).fn));
    await render(<OrgView projectId="p1" live={liveMap} lines={() => <div />} />);
    await act(async () => { document.querySelector<HTMLElement>('[data-testid="ag-zones-team-engineering"]')!.click(); });
    let r = audit();
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
    await act(async () => { document.querySelector<HTMLElement>('[data-testid="ag-org-flow"]')!.click(); });
    await settle();
    r = audit();
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
  });

  it("Directory", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({})));
    const a = (id: string, over: Partial<V2Agent> = {}) => ({
      id, name: id, role: id, status: "IDLE", task: "", model: null, tokens: null, spend_usd: null, calls: 2,
      autonomy_level: 1, trust_score: 80, successful_runs: 3, violations: 0, hidden_failures: 0,
      last_seen_seconds: null, run_id: null, project: null, last_touched: "Last: Reviewing · Build · 2h", registered: true, ...over,
    }) as V2Agent;
    await render(<DirectoryView projectId="p1" actor="ceo" everyone={[a("qa"), a("pm"), a("ghost", { registered: false })]}
                                retired={[a("old")]} live={new Map()} search="" onCreate={() => {}} />);
    const r = audit();
    expect(r.surfaces).toBeGreaterThanOrEqual(2);
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
  });

  it("Activity, folds open, and its empty state", async () => {
    const now = new Date().toISOString();
    vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
      const url = String(input);
      if (url.includes("/tools")) return Response.json({ items: [{ tool_name: "read_file", ok: true, latency_ms: 30 }], more: true });
      if (url.includes("/v1/agents/activity")) {
        return Response.json({ items: [{ act_id: "a1", role_key: "software_engineer", kind: "step", status: "complete",
          how: "model_pick", edge_label: "Picked by BOSS over 3", label: "Build · Ship it #182 with a long label", verb: "Coding",
          links: { workflow_run_id: "wfr_1" }, children: { count: 2 }, turns: { tool_calls: 3 }, cost_usd: 0.42,
          started_at: now, ended_at: now }], next: "c1" });
      }
      return Response.json({});
    }));
    await render(<ActivityRiver projectId="p1" runId="wfr_1" runLabel="Ship it #182" />);
    await clickAll("[data-testid^='ag-river-tools-'], [data-testid^='ag-river-inside-']");
    let r = audit();
    expect(r.surfaces).toBe(1);
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
    act(() => root?.unmount());
    host?.remove();
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ items: [], next: null })));
    await render(<ActivityRiver projectId="p1" />);
    r = audit();
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
  });
});
