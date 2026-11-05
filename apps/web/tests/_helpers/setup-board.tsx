/**
 * Mount the Repo and models board in happy-dom against a fake /api/setup.
 *
 * `api.calls` records every request (method, op, body); `api.answer` decides the response per op
 * (default: the current view). Nothing reaches a network.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { vi } from "vitest";

import type { SetupView } from "../../lib/setup-types";
import { AreaShownContext } from "../../lib/area-shown";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

export type Call = { method: string; url: string; op: string | null; body: Record<string, unknown> };

export type FakeApi = {
  view: SetupView;
  calls: Call[];
  answer: (call: Call) => { status: number; body: unknown } | null;
  /** Hold every write until `release()` — a request in flight. */
  hold: boolean;
  release: () => void;
};

export function fakeApi(view: SetupView): FakeApi {
  const waiting: Array<() => void> = [];
  const api: FakeApi = {
    view, calls: [], answer: () => null, hold: false,
    release: () => { while (waiting.length) waiting.shift()!(); },
  };
  vi.stubGlobal("fetch", vi.fn(async (input: string | URL, init?: RequestInit) => {
    const url = String(input);
    let body: Record<string, unknown> = {};
    try { body = init?.body ? JSON.parse(String(init.body)) : {}; } catch { body = {}; }
    const call: Call = { method: (init?.method ?? "GET").toUpperCase(), url,
                         op: (body.op as string) ?? null, body };
    api.calls.push(call);
    if (call.method !== "GET" && api.hold) await new Promise<void>((resolve) => waiting.push(resolve));
    const chosen = api.answer(call)
      ?? (call.op === "model_catalogue" || url.includes("catalogue=")
        ? { status: 200, body: { models: ["stub/small", "stub/large"] } }
        : { status: 200, body: api.view });
    return new Response(JSON.stringify(chosen.body), {
      status: chosen.status, headers: { "Content-Type": "application/json" } });
  }));
  return api;
}

export type Mounted = { root: Root; container: HTMLElement; unmount: () => void };

export async function mountBoard(view: SetupView, search = "?area=setup", shown = true,
                                 layout: "classic" | "setup" | "welcome" = "classic"): Promise<Mounted> {
  window.history.replaceState(null, "", `/${search}`);
  const { SetupBoard } = await import("../../app/setup/setup-board");
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(<AreaShownContext.Provider value={shown}><SetupBoard projectId="p" initialView={view} layout={layout} /></AreaShownContext.Provider>);
  });
  await flush();
  return { root, container, unmount: () => { act(() => root.unmount()); container.remove(); } };
}

export async function flush(): Promise<void> {
  for (let i = 0; i < 4; i += 1) {
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }
}

export async function click(el: Element | null): Promise<void> {
  if (!el) throw new Error("nothing to click");
  await act(async () => { (el as HTMLElement).click(); });
  await flush();
}

export async function type(el: Element | null, value: string): Promise<void> {
  if (!el) throw new Error("nothing to type into");
  const input = el as HTMLInputElement;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), "value")?.set;
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

export async function press(el: Element | null, key: string): Promise<void> {
  if (!el) throw new Error("nothing to press");
  await act(async () => {
    el.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
  });
  await flush();
}

export const q = (root: ParentNode, testid: string) =>
  root.querySelector(`[data-testid="${testid}"]`);

/** One Setup row on its own, in the board's context (BOSSAPP-898: rows that left Setup's page now
 *  live in Manage panels, Govern and Knowledge, so a row's own behaviour is tested by itself). */
export async function mountInBoard(view: SetupView, element: React.ReactElement, shown = true): Promise<Mounted> {
  const { BoardContext } = await import("../../app/setup/parts");
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  const Host = () => {
    const [v, setV] = React.useState(view);
    const [checking, setChk] = React.useState<Set<string>>(new Set());
    const setChecking = (k: string, on: boolean) => setChk((p) => { const n = new Set(p); if (on) n.add(k); else n.delete(k); return n; });
    return (
      <AreaShownContext.Provider value={shown}>
        <BoardContext.Provider value={{ projectId: "p", view: v, apply: setV, checking, setChecking }}>{element}</BoardContext.Provider>
      </AreaShownContext.Provider>
    );
  };
  await act(async () => { root.render(<Host />); });
  await flush();
  return { root, container, unmount: () => { act(() => root.unmount()); container.remove(); } };
}
