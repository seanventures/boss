// @vitest-environment happy-dom
/**
 * WHAT A WORK ITEM CARRIES BEYOND ITS STATUS (BOSSAPP-880..883), in the work tree's opened row:
 * a person's watchdog (which stops to be told about; Stop removes it).
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SearchResults } from "../app/projects/search-results";
import { WatchSection } from "../app/projects/work-item-extras";
import { parseSearch, parseWatchdogs } from "../lib/work-ux";
import { $, mount, settle, unmount } from "./_helpers/live-render";

type Call = { url: string; method: string; body: unknown };
afterEach(() => { unmount(); vi.restoreAllMocks(); });

function serve(calls: Call[], answer: (url: string, method: string) => unknown) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    calls.push({ url: String(url), method, body: init?.body ? JSON.parse(String(init.body)) : null });
    return new Response(JSON.stringify(answer(String(url), method)), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}

const DOGS = { watchdogs: [{ id: "wdg_1", watcher: "person:ceo", conditions: ["blocked", "closed", "bogus"],
  stall_hours: 24, instructions: "", enabled: true, fired_count: 2 }] };

describe("the watchdog on a work tree", () => {
  it("parses defensively", () => {
    const [d] = parseWatchdogs(DOGS);
    expect(d.conditions).toEqual(["blocked", "closed"]);
    expect(parseWatchdogs({ watchdogs: [{}] })).toEqual([]);
  });

  it("lists who watches, Watch sends the ticked stops, Stop removes one", async () => {
    const calls: Call[] = [];
    serve(calls, (url, method) => (method === "GET" ? DOGS : { watchdog: { id: "wdg_2" } }));
    await mount(<WatchSection projectId="p1" id="w2" demo={false} />);
    await settle();
    expect($('[data-testid="wt-watchdog-wdg_1"]')?.textContent).toContain("Gets blocked · Is closed");
    expect($('[data-testid="wt-watchdog-wdg_1"]')?.textContent).toContain("told 2×");
    ($('[data-testid="wt-watch-stalled"]') as HTMLInputElement).click();
    ($('[data-testid="wt-watch-blocked"]') as HTMLInputElement).click();
    await settle();
    ($('[data-testid="wt-watch-save"]') as HTMLButtonElement).click();
    await settle();
    const sent = calls.find((c) => c.method === "POST" && c.url === "/v1/work-items/w2/watchdogs");
    expect(sent?.body).toEqual({ project_id: "p1", conditions: ["stopped_unfinished", "stalled"], instructions: "" });
    ($('[data-testid="wt-unwatch"]') as HTMLButtonElement).click();
    await settle();
    expect(calls.some((c) => c.method === "DELETE" && c.url.startsWith("/v1/work-items/w2/watchdogs/wdg_1?project_id=p1"))).toBe(true);
  });
});

describe("⌘K Found in (one search over work)", () => {
  it("asks /v1/search for the open project and draws each hit as a palette item", async () => {
    const calls: Call[] = [];
    serve(calls, () => ({ items: [
      { kind: "meeting", id: "mrun_1", title: "Checkout PRD", snippet: "Accept Apple Pay at checkout", href: "?project_id=p1&area=meetings", match: "body" },
      { kind: "decision", id: "dec_1", title: "Ship Apple Pay?", snippet: "", href: "?project_id=p1&area=decisions", match: "title" },
      { kind: "run", id: "", title: "dropped: no id", href: "?x" },
    ], searched: ["meeting", "decision"] }));
    expect(parseSearch({ items: [{ kind: "run", id: "r", href: "" }] }).items).toEqual([]);
    await mount(<SearchResults query="apple pay" projectId="p1" onDone={() => {}} />);
    await new Promise((r) => setTimeout(r, 260));
    await settle();
    expect(calls[0]?.url).toBe("/v1/search?project_id=p1&q=apple%20pay&limit=12");
    expect($('[data-testid="palette-search-meeting"]')?.textContent).toContain("Accept Apple Pay at checkout");
    expect($('[data-testid="palette-search-decision"]')?.className).toContain("bx-palette-item");
  });

  it("a one-letter query asks nothing", async () => {
    const calls: Call[] = [];
    serve(calls, () => ({ items: [] }));
    await mount(<SearchResults query="a" projectId="p1" onDone={() => {}} />);
    await new Promise((r) => setTimeout(r, 260));
    expect(calls).toEqual([]);
    expect($('[data-testid="palette-search"]')).toBeNull();
  });
});
