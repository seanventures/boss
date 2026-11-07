// @vitest-environment happy-dom
/**
 * The flow chart fills its frame (operator, 2026-11-07: "in the video it was full screen, this one
 * is not"): on a 2260×1340 screen the chart drew at 100% in the middle of an empty panel, its
 * labels cut ("esbuild 0.21.5…", "Hand to Security..", "1 findina") and a big empty area below.
 *
 * Now the chart zooms to fit the frame's width and the viewport's height below it (centred, up
 * to FIT.max so a node never grows huge, down to FIT.min and then the frame scrolls), re-fits
 * whenever the window or the frame changes size, and every node carries its full words as a
 * tooltip. The chart's words are sized against a fixed base, never the wide-screen root.
 */
import React from "react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { setConsoleLive } from "../../lib/console-live";
import { NODE_H, NODE_W, layoutFlow } from "../../lib/ambient-layout";
import { FIT, fitScale } from "../../app/agent-ops/flow-canvas";
import { $$, mountAs, settle, tid, unmount } from "../_helpers/multirepo-fixtures";
import { fakeAmbientApi } from "./_fixtures";

const { AgentOpsHome } = await import("../../app/agent-ops/agent-ops-home");

let frameW = 1200;
let viewportH = 900;
const observers = new Set<() => void>();
const realRO = (globalThis as { ResizeObserver?: unknown }).ResizeObserver;
const realWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");

beforeEach(() => {
  (globalThis as { ResizeObserver?: unknown }).ResizeObserver = class {
    cb: () => void; constructor(cb: () => void) { this.cb = cb; }
    observe() { observers.add(this.cb); } disconnect() { observers.delete(this.cb); } };
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get(this: HTMLElement) {
    return this.classList.contains("aa-canvas-frame") ? frameW : 0;
  } });
  Object.defineProperty(window, "innerHeight", { configurable: true, value: viewportH, writable: true });
});
afterEach(() => {
  unmount(); observers.clear(); window.history.replaceState({}, "", "/");
  (globalThis as { ResizeObserver?: unknown }).ResizeObserver = realRO;
  if (realWidth) Object.defineProperty(HTMLElement.prototype, "clientWidth", realWidth);
});

async function openItem() {
  fakeAmbientApi();
  setConsoleLive(false);
  window.history.replaceState({}, "", "/?area=agent_ops&item=work_order_api");
  await mountAs(<AgentOpsHome projectId="p1" actor="ceo" />, "owner");
  await settle(); await settle();
}
const resize = async (w: number, h: number) => {
  frameW = w; viewportH = h;
  Object.defineProperty(window, "innerHeight", { configurable: true, value: h, writable: true });
  await act(async () => { window.dispatchEvent(new Event("resize")); for (const cb of observers) cb(); });
  await settle();
};
const scale = () => Number(tid("aa-canvas")!.getAttribute("data-scale"));

describe("the flow chart zooms to fit its frame", () => {
  it("fits both the width and the height, never past FIT.max nor below FIT.min", () => {
    expect(fitScale(1000, 300, 2000, 1000)).toBe(FIT.max);                       // a small chart on a big screen: capped
    expect(fitScale(1000, 300, 1402, 1000)).toBeCloseTo(1.4, 5);                 // width binds
    expect(fitScale(1000, 600, 2000, 722)).toBeCloseTo(1.2, 5);                  // height binds
    expect(fitScale(1000, 300, 402, null)).toBe(FIT.min);                        // a phone: legible, then it scrolls
    expect(fitScale(1000, 300, 0, null)).toBe(1);                                // not measured yet
  });

  it("a wide screen draws the chart larger and fills the height below it; a resize re-fits it", async () => {
    frameW = 1200; viewportH = 900;
    await openItem();
    const small = scale();
    await resize(1950, 1340);
    const big = scale();
    expect(big).toBeGreaterThan(small);
    expect(big).toBeGreaterThan(1);
    expect(big).toBeLessThanOrEqual(FIT.max);
    const frame = tid("aa-canvas")!;
    expect(frame.hasAttribute("data-fill")).toBe(true);
    expect(parseFloat(frame.style.height)).toBeGreaterThan(0);
    // The drawn chart is never taller than its frame (no clipping) and is scaled, not cropped.
    const fit = frame.querySelector(".aa-canvas-fit") as HTMLElement;
    expect(parseFloat(fit.style.height)).toBeLessThanOrEqual(parseFloat(frame.style.height));
    const t = (frame.querySelector(".aa-canvas") as HTMLElement).style.transform;
    expect(Number(t.match(/^scale\(([\d.]+)\)$/)?.[1])).toBeCloseTo(big, 2);
    // And back down: a phone hugs the chart (no forced height) at a legible zoom.
    await resize(358, 844);
    expect(scale()).toBe(FIT.min);
    expect(tid("aa-canvas")!.hasAttribute("data-fill")).toBe(false);
  });

  it("every node carries its full words as a tooltip", async () => {
    await openItem();
    for (const n of $$('[data-testid="aa-node"]')) {
      const label = n.querySelector('[data-testid="aa-node-label"]')!.textContent ?? "";
      expect(n.getAttribute("title")?.startsWith(label), `${label} → ${n.getAttribute("title")}`).toBe(true);
    }
  });

  it("node boxes hold three lines of label and a chip row at the chart's own fixed type size", () => {
    const css = readFileSync(join(__dirname, "../../app/agent-ops/agent-ops.css"), "utf8");
    expect(css).toMatch(/\.aa-canvas \{[^}]*font-size: 1rem;/);
    expect(css).toMatch(/\.aa-canvas \.aa-node-label \{ font-size: 0\.658em; \}/);
    expect(css).toMatch(/-webkit-line-clamp: 3;[^}]*overflow-wrap: anywhere;/);
    // 3 × (0.658 × 19 × 1.18) label + gap + chip row + padding fit inside a node.
    const lines = 3 * 0.658 * 19 * 1.18;
    expect(lines + 4 + 0.526 * 19 * 1.3 + 14 + 3).toBeLessThanOrEqual(NODE_H);
    const l = layoutFlow([{ id: "a", type: "signal", label: "a", badges: [] }] as never, []);
    expect(l.nodes[0].w).toBe(NODE_W);
  });
});
