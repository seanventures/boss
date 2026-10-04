/**
 * Render Agents › Live straight from a fixture (no poll), for the Live tests.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { parseLiveBoard, type LiveBoardState } from "../../lib/agents-live-board";

let root: Root | null = null;
let host: HTMLElement | null = null;

export async function settle(): Promise<void> {
  for (let i = 0; i < 10; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

export function stateOf(body: Record<string, unknown> | null, over: Partial<LiveBoardState> = {}): LiveBoardState {
  return { board: body ? parseLiveBoard(body) : null, error: null, loading: false, refresh: () => {}, ...over };
}

export async function mount(node: React.ReactElement): Promise<void> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  await settle();
}

export async function rerender(node: React.ReactElement): Promise<void> {
  await act(async () => root!.render(node));
  await settle();
}

export function unmount(): void {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.body.innerHTML = "";
}

export const $ = (sel: string) => document.querySelector<HTMLElement>(sel);
export const $$ = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)];

export async function click(el: Element | null): Promise<void> {
  if (!el) throw new Error("nothing to click");
  await act(async () => { (el as HTMLElement).click(); });
  await settle();
}
