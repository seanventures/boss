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

/**
 * What a real browser's constraint validation would refuse in this form (happy-dom checks none
 * of it): a number outside min/max or off its `step` grid, an empty required field. A browser
 * refuses such a submit before `onSubmit` runs — the "Save does nothing" class — unless the form
 * says `noValidate`. Returns the offending fields' names.
 */
export function browserWouldRefuse(form: HTMLFormElement): string[] {
  if (form.noValidate || form.hasAttribute("novalidate")) return [];
  const bad: string[] = [];
  for (const el of form.querySelectorAll<HTMLInputElement>("input, select, textarea")) {
    const name = el.getAttribute("aria-label") || el.name || el.getAttribute("data-testid") || el.tagName;
    const value = el.value;
    if (el.hasAttribute("required") && !value) { bad.push(name); continue; }
    if (el.type !== "number" || value === "") continue;
    const n = Number(value);
    const min = el.getAttribute("min"), max = el.getAttribute("max"), step = el.getAttribute("step") ?? "1";
    if (!Number.isFinite(n) || (min !== null && n < Number(min)) || (max !== null && n > Number(max))) { bad.push(name); continue; }
    if (step !== "any") {
      const units = (n - Number(min ?? 0)) / Number(step);
      if (Math.abs(units - Math.round(units)) > 1e-7) bad.push(name);
    }
  }
  return bad;
}

export async function click(el: Element | null): Promise<void> {
  if (!el) throw new Error("nothing to click");
  const button = el as HTMLButtonElement;
  const form = button.tagName === "BUTTON" && (button.type || "submit") === "submit" ? button.closest("form") : null;
  if (form && browserWouldRefuse(form).length) {
    // As a browser would: no submit event. The test then sees exactly what the operator saw.
    await settle();
    return;
  }
  await act(async () => { (el as HTMLElement).click(); });
  await settle();
}
