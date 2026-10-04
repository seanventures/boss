// @vitest-environment happy-dom
/**
 * Agents › Activity (agents-home spec §4.9, §8.2 activity-river): a river of agent work.
 * Rendered against a stubbed /v1/agents/activity and asserted on what is drawn:
 *   - rows grouped by day (`Today`, `Yesterday`, `Mon 29 Sep`), newest first, absolute times;
 *   - the sentence is `<Name> <verb lower> <label>`, from data;
 *   - `+n inside` and `n tool calls` fold, and an item with none shows no control;
 *   - tool rows say name · ok/failed · latency, never content;
 *   - new work waits behind `n new ↑` and never shifts the rows being read;
 *   - filters, the window and the run chip set the query; `Load more` pages by cursor.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { ActivityRiver, dayLabel, RIVER_POLL_MS } = await import("../app/agents/activity-river");

const iso = (minutesAgo: number) => new Date(Date.now() - minutesAgo * 60_000).toISOString();
const item = (id: string, over: Record<string, unknown> = {}) => ({
  act_id: id, role_key: "software_engineer", kind: "stage", status: "complete", how: "next_stage",
  edge_label: "Next stage", label: "Build · Ship it #182", verb: "Coding", root_act_id: "r1",
  links: { workflow_run_id: "wfr_1" }, children: { count: 0 }, turns: { tool_calls: 0 }, cost_usd: 0,
  started_at: iso(5), ended_at: iso(3), ...over,
});

let pages: Record<string, unknown>[] = [];
let calls: string[] = [];
let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => {
  calls = [];
  pages = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const url = String(input);
    calls.push(url);
    if (url.includes("/tools")) {
      return Response.json({ items: [{ tool_name: "read_file", ok: true, latency_ms: 340, content: "SECRET" },
                                     { tool_name: "run_tests", ok: false, latency_ms: 1200 }], more: true });
    }
    if (url.includes("/v1/agents/activity")) {
      if (url.includes("run=wfr_1") && url.includes("limit=200")) {
        return Response.json({ items: [item("k1", { parent_act_id: "a_parent", verb: "Reviewing", label: "Code review · Ship it" }),
                                       item("k2", { parent_act_id: "other" })], next: null });
      }
      if (url.includes("cursor=")) return Response.json({ items: [item("old1", { started_at: iso(60 * 24 * 6) })], next: null });
      return Response.json(pages.shift() ?? { items: [], next: null });
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
const $$ = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)];

describe("Activity: the river", () => {
  it("groups by day, reads as a sentence, and keeps absolute times", async () => {
    const yesterday = new Date();
    yesterday.setDate(yesterday.getDate() - 1);
    yesterday.setHours(12, 0, 0, 0);
    const today = new Date();
    today.setHours(0, 1, 0, 0);
    const a1 = item("a1", { started_at: today.toISOString(), ended_at: new Date(today.getTime() + 120_000).toISOString() });
    pages = [{ items: [a1, item("a2", { started_at: yesterday.toISOString(), ended_at: yesterday.toISOString() })],
               next: null }];
    await render(<ActivityRiver projectId="p1" />);
    expect($$(".ag-river-dayhead").map((h) => h.textContent)).toEqual(["Today", "Yesterday"]);
    const row = $('[data-testid="ag-river-row-a1"]')!;
    expect(row.querySelector(".ag-river-text")!.textContent).toBe("Forge coding Build · Ship it #182");
    const time = row.querySelector("time")!;
    expect(time.textContent).toMatch(/^\d\d:\d\d$/);
    expect(time.getAttribute("title")).toBe(a1.started_at);
    expect(row.querySelector(".ag-river-status")!.textContent).toBe("Done");
    expect(row.querySelector(".ag-river-took")!.textContent).toBe("2m");
    expect(dayLabel("2026-09-29T10:00:00", new Date("2026-10-04T10:00:00"))).toBe("Tue 29 Sep");
  });

  it("a handoff carries the edge glyph with the server's words; work a workflow named does not", async () => {
    pages = [{ items: [item("h1", { how: "model_pick", edge_label: "Picked by BOSS over 3" }), item("n1", { how: "named" })],
               next: null }];
    await render(<ActivityRiver projectId="p1" />);
    expect($('[data-testid="ag-river-row-h1"] .ag-river-edge')!.getAttribute("title")).toBe("Picked by BOSS over 3");
    expect($('[data-testid="ag-river-row-n1"] .ag-river-edge')).toBeNull();
  });

  it("folds children and tool calls; nothing to fold shows no control", async () => {
    pages = [{ items: [item("a_parent", { kind: "step", children: { count: 1 }, turns: { tool_calls: 2 } }), item("plain")],
               next: null }];
    await render(<ActivityRiver projectId="p1" />);
    expect($('[data-testid="ag-river-inside-plain"]')).toBeNull();
    expect($('[data-testid="ag-river-tools-plain"]')).toBeNull();
    const inside = $('[data-testid="ag-river-inside-a_parent"]')!;
    expect(inside.textContent).toContain("+1 inside");
    await act(async () => { inside.click(); });
    await settle();
    expect($('[data-testid="ag-river-row-k1"]')).not.toBeNull();
    expect($('[data-testid="ag-river-row-k2"]')).toBeNull();
    const tools = $('[data-testid="ag-river-tools-a_parent"]')!;
    expect(tools.textContent).toContain("2 tool calls");
    await act(async () => { tools.click(); });
    await settle();
    const list = $('[data-testid="ag-river-toollist-a_parent"]')!;
    expect(list.textContent).toContain("read_file · ok · 340ms");
    expect(list.textContent).toContain("run_tests · failed · 1200ms");
    expect(list.textContent).not.toContain("SECRET");
    const log = $('[data-testid="ag-river-log-a_parent"]')!;
    expect(log.textContent).toBe("Open in Activity log ↗");
    expect(log.getAttribute("href")).toContain("area=activity");
    expect(log.getAttribute("href")).toContain("workflow_run_id=wfr_1");
  });

  it("new work waits behind `n new ↑` and does not shift the rows", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    pages = [{ items: [item("a1"), item("a2")], next: null },
             { items: [item("n1", { started_at: iso(0) }), item("a1"), item("a2")], next: null }];
    await render(<ActivityRiver projectId="p1" />);
    const before = $$(".ag-river-row").map((r) => r.dataset.testid);
    await act(async () => { vi.advanceTimersByTime(RIVER_POLL_MS); });
    await settle();
    expect($$(".ag-river-row").map((r) => r.dataset.testid)).toEqual(before);
    const pill = $('[data-testid="ag-river-new"]')!;
    expect(pill.textContent).toBe("1 new ↑");
    await act(async () => { pill.click(); });
    expect($$(".ag-river-row")[0].dataset.testid).toBe("ag-river-row-n1");
    expect($('[data-testid="ag-river-new"]')).toBeNull();
  });

  it("does not poll while the console is paused", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    pages = [{ items: [item("a1")], next: null }];
    await render(<ActivityRiver projectId="p1" paused />);
    const n = calls.length;
    await act(async () => { vi.advanceTimersByTime(RIVER_POLL_MS * 3); });
    await settle();
    expect(calls.length).toBe(n);
  });

  it("filters, the window, the agent and the run set the query", async () => {
    pages = [{ items: [item("a1")], next: null }];
    const onClear = vi.fn();
    await render(<ActivityRiver projectId="p1" roleKey="qa" runId="wfr_9" runLabel="Ship it #9" onClearRun={onClear} />);
    expect(calls[0]).toContain("role=qa");
    expect(calls[0]).toContain("run=wfr_9");
    expect(calls[0]).toContain("days=1");
    expect($$(".ag-river-filters button").map((b) => b.textContent)).toEqual(
      ["All", "Needs you", "Handoffs", "Builds", "Panels", "Talks"]);
    await act(async () => { $('[data-testid="ag-river-filter-handoffs"]')!.click(); });
    await settle();
    expect(calls.at(-1)).toContain("kinds=handoffs");
    await act(async () => { $('[data-testid="ag-river-days-7"]')!.click(); });
    await settle();
    expect(calls.at(-1)).toContain("days=7");
    expect($('[data-testid="ag-river-run"]')!.textContent).toContain("Ship it #9");
    await act(async () => { $('[data-testid="ag-river-run"]')!.click(); });
    expect(onClear).toHaveBeenCalled();
  });

  it("pages by cursor with Load more, and says when there is nothing yet", async () => {
    pages = [{ items: [item("a1")], next: "2026-10-04T09:00:00Z|a1" }];
    await render(<ActivityRiver projectId="p1" />);
    await act(async () => { $('[data-testid="ag-river-more"]')!.click(); });
    await settle();
    expect(calls.some((c) => c.includes("cursor=2026-10-04T09%3A00%3A00Z%7Ca1"))).toBe(true);
    expect($('[data-testid="ag-river-row-old1"]')).not.toBeNull();
    expect($('[data-testid="ag-river-more"]')).toBeNull();
    act(() => root?.unmount());
    host?.remove();
    pages = [{ items: [], next: null }];
    await render(<ActivityRiver projectId="p1" />);
    const empty = $('[data-testid="ag-river-empty"]')!;
    expect(empty.textContent).toContain("No agent work yet");
    expect(empty.textContent).toContain("Start work ↗");
  });
});
