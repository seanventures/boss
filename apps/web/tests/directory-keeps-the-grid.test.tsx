// @vitest-environment happy-dom
/**
 * Agents › Directory keeps the grid (agents-home spec §4.8, §8.2 directory-keeps-the-grid):
 * the same cards and testids, grouped by TEAM in the shipped order (empty teams hidden), no
 * `Needs you` chip (Live owns what is waiting), and no "calls in 30d" volume on an idle card.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { V2Agent } from "../lib/v2/workforce";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
vi.mock("../app/focused-agents-ui", () => ({ FocusedAgents: () => <div data-testid="focused-agents-stub" /> }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { DirectoryView } = await import("../app/agents/directory-view");
const { primeIdentities, resetIdentities } = await import("../lib/agent-identity");

const agent = (id: string, over: Partial<V2Agent> = {}): V2Agent => ({
  id, name: id.replace(/_/g, " "), role: id, status: "IDLE", task: "last: implement · 12 calls in 30d", model: null,
  tokens: null, spend_usd: null, calls: 12, autonomy_level: 1, trust_score: 50, successful_runs: 0, violations: 0,
  hidden_failures: 0, last_seen_seconds: null, run_id: null, project: null, last_touched: null, registered: true,
  ...over,
} as V2Agent);

let root: Root | null = null;
let host: HTMLElement | null = null;
beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({})));
});
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.body.innerHTML = "";
  resetIdentities();
  vi.unstubAllGlobals();
});
async function render(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
const $ = (sel: string) => document.querySelector<HTMLElement>(sel);
const $$ = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)];

/** Teach the identity store each agent's team, as /v1/agents/identities does. */
function seedTeams(teams: Record<string, string>): void {
  primeIdentities(Object.entries(teams).map(([role_key, team]) => ({ role_key, display_name: role_key, team })));
}

describe("Directory", () => {
  it("groups the same cards by team, in the shipped order, hiding empty teams", async () => {
    seedTeams({ qa: "quality", software_engineer: "engineering", pm: "leadership", visionary_founder: "thinkers",
                my_agent: "" });
    const everyone = [agent("qa"), agent("software_engineer"), agent("pm"), agent("visionary_founder"),
                      agent("my_agent"), agent("ghost", { registered: false })];
    await render(<DirectoryView projectId="p1" actor="ceo" everyone={everyone} retired={[]} live={new Map()}
                                search="" onCreate={() => {}} />);
    expect($('[data-testid="roster-grid"]')!.dataset.group).toBe("team");
    expect($$("[data-testid^='ag-dir-team-']").map((s) => s.dataset.team))
      .toEqual(["leadership", "engineering", "quality", "thinkers", "yours"]);
    expect($('[data-testid="ag-dir-team-yours"]')!.querySelectorAll("article")).toHaveLength(2);
    expect($('[data-testid="ag-dir-team-thinkers"] .ag-zone-label')!.getAttribute("title"))
      .toBe("Inspired by a way of thinking. Not real people.");
    // Every testid the cards had.
    for (const id of ["qa", "software_engineer"]) {
      expect($(`[data-testid="wf-open-${id}"]`)).not.toBeNull();
      expect($(`[data-testid="wf-ask-${id}"]`)).not.toBeNull();
    }
    expect($('[data-testid="wf-register-ghost"]')).not.toBeNull();
  });

  it("keeps its filters, without Needs you", async () => {
    await render(<DirectoryView projectId="p1" actor="ceo" everyone={[agent("qa"), agent("pm", { calls: 0, task: "" }),
                                                                       agent("ghost", { registered: false })]}
                                retired={[agent("old")]} live={new Map([["qa", { state: "needs_you", line: "x" }]])}
                                search="" onCreate={() => {}} />);
    expect($$("[data-testid^='wf-filter-']").map((b) => b.textContent))
      .toEqual(["All 3", "On duty", "Has done work 2", "Retired 1"]);
    expect($('[data-testid="wf-filter-needs_you"]')).toBeNull();
    expect($('[data-testid="roster-ungoverned"]')!.textContent).toBe("1 unregistered");
    await act(async () => { $('[data-testid="wf-filter-retired"]')!.click(); });
    expect($('[data-testid="wf-agent-old"]')).not.toBeNull();
  });

  it("an idle card never shows a call count", async () => {
    await render(<DirectoryView projectId="p1" actor="ceo" everyone={[agent("qa"),
                                                                       agent("pm", { last_touched: "Last: Planning · PRD · 2h" })]}
                                retired={[]} live={new Map()} search="" onCreate={() => {}} />);
    expect(document.body.textContent).not.toMatch(/calls? in 30d/);
    expect($('[data-testid="ag-line-qa"]')!.textContent).toBe("No work yet");
    expect($('[data-testid="ag-line-pm"]')!.textContent).toBe("Last: Planning · PRD · 2h");
  });

  it("the header search filters the grid", async () => {
    await render(<DirectoryView projectId="p1" actor="ceo" everyone={[agent("qa"), agent("pm")]} retired={[]}
                                live={new Map()} search="pm" onCreate={() => {}} />);
    expect($('[data-testid="wf-agent-pm"]')).not.toBeNull();
    expect($('[data-testid="wf-agent-qa"]')).toBeNull();
  });
});
