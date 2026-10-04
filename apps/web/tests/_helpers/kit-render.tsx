/** Render, settle and query helpers for the Skills & MCP tests (happy-dom). */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;

export async function settle(n = 15) {
  for (let i = 0; i < n; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

export async function render(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  await settle();
  return host;
}

export function cleanup() {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.body.innerHTML = "";
}

export const $ = (sel: string) => document.querySelector<HTMLElement>(sel);
export const $$ = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)];

export async function click(sel: string | HTMLElement, init: MouseEventInit = {}) {
  const el = typeof sel === "string" ? $(sel) : sel;
  if (!el) throw new Error(`nothing matches ${String(sel)}`);
  await act(async () => { el.dispatchEvent(new MouseEvent("click", { bubbles: true, ...init })); });
  await settle();
}

export async function type(sel: string, value: string) {
  const el = $(sel) as HTMLInputElement | null;
  if (!el) throw new Error(`nothing matches ${sel}`);
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value")?.set;
    setter?.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await settle(3);
}
