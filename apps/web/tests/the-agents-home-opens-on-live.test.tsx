// @vitest-environment happy-dom
/**
 * Agents opens on Live (agents-home spec §3.1). The old Roster is Directory and the old Team is
 * Organization: an old link lands on the new tab and the URL is rewritten to say so. `?role=`
 * still opens the Passport, over Live.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Workforce, V2Agent } from "../lib/v2/workforce";
import { board, liveFetch } from "./_helpers/live-board-fixture";
import { $, click, mount, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { WorkforceArea } = await import("../app/workforce-ui");
const { resetLiveStores } = await import("../lib/agents-live");

const agent = (id: string): V2Agent => ({
  id, name: id, role: id, status: "IDLE", task: "", model: null, tokens: null, spend_usd: null, calls: 0,
  autonomy_level: 1, trust_score: 50, successful_runs: 0, violations: 0, hidden_failures: 0,
  last_seen_seconds: null, run_id: null, project: null, last_touched: null, registered: true,
} as V2Agent);
const workforce: Workforce = {
  project_id: "p1", window_hours: 24, apiReachable: true, handoffs: [],
  counts: { WORKING: 0, STUCK: 0, IDLE: 2, total: 2 }, agents: [agent("qa"), agent("pm")],
} as Workforce;

/** The Passport's own read, enough for it to draw. */
const DETAIL = {
  role_key: "qa", name: "QA Lead", registered: true, autonomy_level: 1, trust_score: 50, successful_runs: 0,
  violations: 0, hidden_failures: 0, reports_to: null, spec: {}, owns_stages: [], apps: [], panels: [],
  assigned_to: [], named_on_steps: [], window_hours: 168,
};
function withPassport(inner: (input: unknown) => Promise<Response>) {
  return async (input: unknown) => (String(input).startsWith("/api/agents/") ? Response.json(DETAIL) : inner(input));
}

beforeEach(() => { vi.stubGlobal("fetch", vi.fn(withPassport(liveFetch(() => board())))); });
afterEach(() => { unmount(); resetLiveStores(); vi.unstubAllGlobals(); });

const view = () => $('[data-testid="agents-area"]')?.dataset.view;
const param = (k: string) => new URLSearchParams(window.location.search).get(k);

describe("the Agents home", () => {
  it("opens on Live", async () => {
    window.history.replaceState(null, "", "/?area=workforce");
    await mount(<WorkforceArea workforce={workforce} actor="ceo" />);
    expect(view()).toBe("live");
    expect($('[data-testid="ag-live"]')).not.toBeNull();
    expect($('[data-testid="agents-view-live"]')?.getAttribute("aria-selected")).toBe("true");
    expect($('[data-testid="agents-view-live"]')?.getAttribute("title")).toBe("What needs you and what's moving");
  });

  it("an old Roster link lands on Directory, and the URL says so", async () => {
    window.history.replaceState(null, "", "/?area=workforce&view=roster");
    await mount(<WorkforceArea workforce={workforce} actor="ceo" />);
    expect(view()).toBe("directory");
    expect(param("view")).toBe("directory");
    expect($('[data-testid="wf-filter-all"]')?.textContent).toBe("All 2");
  });

  it("an old Team link lands on Organization, and the URL says so", async () => {
    window.history.replaceState(null, "", "/?area=workforce&view=team");
    await mount(<WorkforceArea workforce={workforce} actor="ceo" />);
    expect(view()).toBe("org");
    expect(param("view")).toBe("org");
  });

  it("?role= opens the Passport over Live", async () => {
    window.history.replaceState(null, "", "/?area=workforce&role=qa");
    await mount(<WorkforceArea workforce={workforce} actor="ceo" />);
    expect(view()).toBe("live");
    expect($('[data-testid="agent-detail"]')).not.toBeNull();
  });

  it("the tabs are a tablist the arrow keys move along, and Live is the URL without a view", async () => {
    window.history.replaceState(null, "", "/?area=workforce&view=directory");
    await mount(<WorkforceArea workforce={workforce} actor="ceo" />);
    const tabs = [...document.querySelectorAll<HTMLElement>('[role="tab"]')].map((t) => t.textContent?.trim());
    expect(tabs).toEqual(["Live", "Organization", "Directory (2)", "Activity"]);
    const list = $('[role="tablist"]')!;
    const { act } = await import("react");
    await act(async () => { list.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })); });
    expect(view()).toBe("activity");
    await click($('[data-testid="agents-view-live"]'));
    expect(param("view")).toBeNull();
  });

  it("the header says agents, working and need you, and the Live tab carries a dot", async () => {
    window.history.replaceState(null, "", "/?area=workforce");
    vi.stubGlobal("fetch", vi.fn(liveFetch(() => board(), () => ({
      items: [{ role_key: "software_engineer", state: "working", line: "Now: Coding" }],
      attention: { count: 2, items: [{ id: "dec_gate", role_key: "media_publisher", title: "Publish?", decide_inline: true },
                                     { id: "dec_pr", role_key: null, title: "Merge?", decide_inline: false }] },
    }))));
    await mount(<WorkforceArea workforce={workforce} actor="ceo" />);
    expect($('[data-testid="ag-home-summary"]')?.textContent).toBe("2 agents · 1 working · 2 need you");
    expect($('[data-testid="agents-live-dot"]')?.getAttribute("title")).toBe("2 need you");
  });

  it("Find an agent is on every tab, and Enter opens the one it names", async () => {
    window.history.replaceState(null, "", "/?area=workforce");
    await mount(<WorkforceArea workforce={workforce} actor="ceo" />);
    const input = $('[data-testid="ag-home-search"]') as HTMLInputElement;
    expect(input).not.toBeNull();
    const { act } = await import("react");
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "q");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect($('[data-testid="ag-home-match-qa"]')).not.toBeNull();
    await act(async () => { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    expect(param("role")).toBe("qa");
  });
});
