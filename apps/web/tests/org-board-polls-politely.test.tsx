// @vitest-environment happy-dom
/**
 * The org board polls politely (org-view spec §6.1): every 30s, only while the page is visible
 * and the console is Live; If-None-Match on every repeat, and a 304 keeps what is on screen; a
 * write asks at once past the server's cache (`fresh=1`). Organization never polls the Live
 * board.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { stubMatchMedia } from "./_helpers/agents-home-fixtures";
import { orgWire } from "./_helpers/org-board-fixtures";
import type { Workforce } from "../lib/v2/workforce";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
vi.mock("../app/focused-agents-ui", () => ({ FocusedAgents: () => <div /> }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { useOrgBoard, ORG_POLL_MS } = await import("../lib/agents-org-board");
const { setConsoleLive } = await import("../lib/console-live");
const { notifyChanged } = await import("../app/changed");
const { WorkforceArea } = await import("../app/workforce-ui");

let root: Root | null = null;
let host: HTMLElement | null = null;
let calls: { url: string; headers: Record<string, string> }[] = [];
let answer304 = false;
let state: ReturnType<typeof useOrgBoard> | null = null;

function Probe() {
  state = useOrgBoard("p1", 7, true);
  return <span data-testid="probe">{state.board ? state.board.teams.length : "none"}</span>;
}

beforeEach(() => {
  calls = [];
  answer304 = false;
  state = null;
  stubMatchMedia([]);
  setConsoleLive(true);
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, headers: { ...(init?.headers as Record<string, string> ?? {}) } });
    if (url.startsWith("/v1/agents/org-board")) {
      if (answer304) return new Response(null, { status: 304, headers: { etag: "\"ob-1\"" } });
      return new Response(JSON.stringify(orgWire()), { status: 200, headers: { "content-type": "application/json", etag: "\"ob-1\"" } });
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
  setConsoleLive(true);
});
async function flush() {
  for (let i = 0; i < 8; i += 1) await act(async () => { await Promise.resolve(); });
}
async function mount(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  await flush();
}
const boardCalls = () => calls.filter((c) => c.url.startsWith("/v1/agents/org-board"));

describe("the org board poll", () => {
  it("asks every 30s with If-None-Match, and a 304 keeps the board", async () => {
    vi.useFakeTimers();
    await mount(<Probe />);
    expect(boardCalls()).toHaveLength(1);
    expect(boardCalls()[0].url).toBe("/v1/agents/org-board?project_id=p1&days=7");
    expect(state!.board).not.toBeNull();
    answer304 = true;
    await act(async () => { vi.advanceTimersByTime(ORG_POLL_MS - 1000); });
    await flush();
    expect(boardCalls()).toHaveLength(1);
    await act(async () => { vi.advanceTimersByTime(1000); });
    await flush();
    expect(boardCalls()).toHaveLength(2);
    expect(boardCalls()[1].headers["If-None-Match"]).toBe("\"ob-1\"");
    expect(state!.board).not.toBeNull();
    expect(ORG_POLL_MS).toBe(30_000);
  });

  it("does not ask while paused or hidden, and a change asks at once past the cache", async () => {
    vi.useFakeTimers();
    await mount(<Probe />);
    setConsoleLive(false);
    await act(async () => { vi.advanceTimersByTime(ORG_POLL_MS * 2); });
    await flush();
    expect(boardCalls()).toHaveLength(1);
    setConsoleLive(true);
    await flush();
    const afterResume = boardCalls().length;
    expect(afterResume).toBe(2);
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    await act(async () => { vi.advanceTimersByTime(ORG_POLL_MS * 2); });
    await flush();
    expect(boardCalls()).toHaveLength(afterResume);
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    notifyChanged("test");
    await flush();
    const last = boardCalls().slice(-1)[0];
    expect(last.url).toContain("fresh=1");
    expect(last.headers["If-None-Match"]).toBeUndefined();
  });

  it("Organization never polls the Live board", async () => {
    const workforce = { project_id: "p1", window_hours: 24, apiReachable: true,
      counts: { WORKING: 0, STUCK: 0, IDLE: 0, "AT GATE": 0, total: 0 }, agents: [], handoffs: [] } as unknown as Workforce;
    window.history.replaceState(null, "", "/?area=workforce&view=org");
    await mount(<WorkforceArea workforce={workforce} actor="ceo" initialView="org" />);
    for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(calls.some((c) => c.url.startsWith("/v1/agents/org-board"))).toBe(true);
    expect(calls.some((c) => c.url.startsWith("/v1/agents/live-board"))).toBe(false);
  });
});
