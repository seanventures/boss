// @vitest-environment happy-dom
/**
 * A Journey strip wider than its card says so (stranger test r3, 2026-11-18: at 390 px the strip
 * stopped after "Staging" with nothing to show it scrolls; a phone hides the scrollbar).
 *
 * Breaks if an overflowing strip shows no fade and no "More stages" chevron, if the chevron does
 * not scroll the strip, or if a strip that fits still shows them.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";

import { Journey } from "../app/projects/kit";
import type { Node } from "../lib/project-experience";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => { root?.unmount(); });
  host?.remove();
  host = null; root = null;
});

const NODES: Node[] = ["Onboarded", "Ready", "Backlog", "Working", "Staging", "Production"].map(
  (label) => ({ key: label.toLowerCase(), label, state: "todo", sub: "", href: "" }));

/** Lay out the strip as a browser would: scrollWidth/clientWidth come from the sizes given. */
function sized(scrollWidth: number, clientWidth: number) {
  const proto = HTMLElement.prototype as unknown as Record<string, unknown>;
  const saved = ["scrollWidth", "clientWidth"].map((k) => [k, Object.getOwnPropertyDescriptor(proto, k)] as const);
  Object.defineProperty(proto, "scrollWidth", { configurable: true, get() { return (this as HTMLElement).classList.contains("px-jr") ? scrollWidth : 0; } });
  Object.defineProperty(proto, "clientWidth", { configurable: true, get() { return (this as HTMLElement).classList.contains("px-jr") ? clientWidth : 0; } });
  return () => { for (const [k, d] of saved) { if (d) Object.defineProperty(proto, k, d); else delete proto[k]; } };
}

async function show(): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root!.render(<Journey nodes={NODES} onOpen={() => {}} />); });
  return host;
}

describe("the Journey strip at phone width", () => {
  it("an overflowing strip shows a fade and a More stages chevron that scrolls it", async () => {
    const restore = sized(560, 330);
    try {
      const el = await show();
      const wrap = el.querySelector<HTMLElement>("[data-testid='jr-wrap-px-journey']")!;
      expect(wrap.hasAttribute("data-more-right")).toBe(true);
      expect(wrap.hasAttribute("data-more-left")).toBe(false);
      const more = el.querySelector<HTMLButtonElement>("[data-testid='jr-more-px-journey']")!;
      expect(more.getAttribute("aria-label")).toBe("More stages");
      const strip = el.querySelector<HTMLElement>("[data-testid='px-journey']")!;
      await act(async () => { more.click(); });
      await act(async () => { strip.scrollLeft = 230; strip.dispatchEvent(new Event("scroll")); });
      expect(wrap.hasAttribute("data-more-left")).toBe(true);
      expect(wrap.hasAttribute("data-more-right")).toBe(false);
      expect(el.querySelector("[data-testid='jr-more-px-journey']")).toBeNull();
    } finally { restore(); }
  });

  it("a strip that fits shows neither", async () => {
    const restore = sized(500, 500);
    try {
      const el = await show();
      const wrap = el.querySelector<HTMLElement>("[data-testid='jr-wrap-px-journey']")!;
      expect(wrap.hasAttribute("data-more-right")).toBe(false);
      expect(el.querySelector("[data-testid='jr-more-px-journey']")).toBeNull();
    } finally { restore(); }
  });

  it("the stylesheet fades the hidden edge and places the chevron", () => {
    const css = fs.readFileSync(path.resolve(__dirname, "../app/projects/px.css"), "utf8");
    expect(css).toMatch(/\.px-jr-wrap\[data-more-right\] > \.px-jr \{[^}]*mask-image/);
    expect(css).toMatch(/\.px-jr-more \{[^}]*position: absolute/);
  });
});
