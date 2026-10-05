// @vitest-environment happy-dom
/**
 * The pulse (spec §4.2.4): one bar per server bucket with its own tooltip, a red cap on any
 * bucket with errors, a drag that zooms the range to the dragged buckets, Enter on a bar that
 * zooms to it, double-click back — and no animation under reduced motion.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { mapActivityHistogram } from "../lib/activity-log";
import { histogramOf } from "./_helpers/log-fixtures";
import { $, $$, mount, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { PulseStrip, pulseTip } = await import("../app/activity/pulse-strip");

afterEach(() => unmount());

describe("the pulse is real counts", () => {
  it("draws one bar per bucket, says each one, caps errors in red", async () => {
    const h = mapActivityHistogram(histogramOf([4, 0, 9, 1]))!;
    await mount(<PulseStrip histogram={h} loading={false} onZoom={() => {}} onReset={() => {}} />);
    const bars = $$("[data-testid=lx-bar]");
    expect(bars).toHaveLength(4);
    expect(bars[2].getAttribute("data-errors")).toBe("true");
    expect(bars[2].getAttribute("title")).toBe(pulseTip(h.buckets[2], 900));
    expect(bars[2].getAttribute("title")).toMatch(/^\d\d:\d\d–\d\d:\d\d · 10 lines · 1 error$/);
    expect(bars[1].querySelector(".lx-bar-stack")!.getAttribute("style")).toContain("height: 0%");
  });

  it("an empty range is a flat line that says so", async () => {
    await mount(<PulseStrip histogram={mapActivityHistogram({ ...histogramOf([0, 0]), total: 0 })!}
                            loading={false} onZoom={() => {}} onReset={() => {}} />);
    expect($("[data-testid=lx-pulse-empty]")!.textContent).toBe("No lines in this range");
  });

  it("dragging across bars zooms to them; Enter zooms to one; double-click goes back", async () => {
    const onZoom = vi.fn();
    const onReset = vi.fn();
    const h = mapActivityHistogram(histogramOf([1, 2, 3, 4]))!;
    await mount(<PulseStrip histogram={h} loading={false} onZoom={onZoom} onReset={onReset} />);
    const strip = $(".lx-pulse-bars")!;
    strip.getBoundingClientRect = () => ({ left: 0, width: 400, top: 0, height: 60, right: 400, bottom: 60, x: 0, y: 0, toJSON: () => ({}) });
    await act(async () => {
      strip.dispatchEvent(new PointerEvent("pointerdown", { clientX: 10, button: 0, bubbles: true }));
      strip.dispatchEvent(new PointerEvent("pointermove", { clientX: 250, bubbles: true }));
      strip.dispatchEvent(new PointerEvent("pointerup", { clientX: 250, bubbles: true }));
    });
    expect(onZoom).toHaveBeenCalledWith(h.buckets[0].t, new Date(Date.parse(h.buckets[2].t) + 900_000).toISOString());
    await act(async () => { $$("[data-testid=lx-bar]")[3].dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    expect(onZoom).toHaveBeenLastCalledWith(h.buckets[3].t, new Date(Date.parse(h.buckets[3].t) + 900_000).toISOString());
    await act(async () => { $("[data-testid=lx-pulse]")!.dispatchEvent(new MouseEvent("dblclick", { bubbles: true })); });
    expect(onReset).toHaveBeenCalledTimes(1);
  });

  it("every Log motion has a still under reduced motion", () => {
    const css = readFileSync(resolve(__dirname, "../app/activity/log.css"), "utf8");
    const still = css.slice(css.indexOf("@media (prefers-reduced-motion: reduce)"));
    for (const moving of [".lx-row[data-fresh]", ".lx-new", ".lx-drawer", ".lx-bar-stack"]) {
      expect(still).toContain(moving);
    }
  });
});
