// @vitest-environment happy-dom
/**
 * A bucket with no probes is a gap, and looks like one (spec §4.9, §7): outlined, never green;
 * a bucket where every probe failed is red AND hatched, so colour is never the only signal; the
 * strip is one image with a summary a screen reader can read; `View as table` lists every bucket.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { parsePulseStrip } from "../lib/obs-view";
import { PulseStrip } from "../app/observability/pulse-strip";
import { DOWN_STRIP, FROM_24H, LIVE_STRIP } from "./_helpers/obs-fixtures";
import { $, $$, click, mount, unmount } from "./_helpers/live-render";
import { openObs } from "./_helpers/obs-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const cellsOf = (root: string) => $$(`${root} rect.obs-cell`);

describe("the Pulse strip", () => {
  it("draws a gap as an outlined cell, never green", async () => {
    await mount(<div data-testid="s"><PulseStrip cells={parsePulseStrip(LIVE_STRIP)} fromIso={FROM_24H} cellS={900} label="invoice prod" span="24 hours" /></div>);
    const cells = cellsOf("[data-testid='s']");
    expect(cells).toHaveLength(96);
    expect(cells[40].getAttribute("data-k")).toBe("gap");
    expect(cells[41].getAttribute("data-k")).toBe("gap");
    expect(cells[40].getAttribute("fill")).toBeNull();
    expect(cells[60].getAttribute("data-k")).toBe("some");
    expect(cells.filter((c) => c.getAttribute("data-k") === "ok")).toHaveLength(93);
    expect(cells[40].querySelector("title")?.textContent).toMatch(/No probes$/);
    expect(cells[60].querySelector("title")?.textContent).toMatch(/7 of 8 ok · p95 412 ms$/);
  });

  it("hatches a bucket where every probe failed", async () => {
    await mount(<div data-testid="s"><PulseStrip cells={parsePulseStrip(DOWN_STRIP)} fromIso={FROM_24H} cellS={900} label="api prod" span="24 hours" /></div>);
    const cells = cellsOf("[data-testid='s']");
    const down = cells.slice(91);
    expect(down.every((c) => c.getAttribute("data-k") === "none")).toBe(true);
    expect(down.every((c) => /^url\(#obs-hatch-/.test(c.getAttribute("fill") ?? ""))).toBe(true);
    expect(cells[90].getAttribute("data-k")).toBe("ok");
  });

  it("says what it shows in one line", async () => {
    await mount(<div data-testid="s"><PulseStrip cells={parsePulseStrip(LIVE_STRIP)} fromIso={FROM_24H} cellS={900} label="invoice prod" span="24 hours"
                                                  pins={[{ at: "2026-10-04T02:12:00Z", sha7: "a1b2c3d", word: "Live", tone: "green" }]} /></div>);
    const img = $("[data-testid='s'] svg.obs-cells[role='img']");
    // 94 cells × 8 probes, one of them 7 ok: 751 of 752.
    expect(img?.getAttribute("aria-label")).toBe("invoice prod, 24 hours: up 99.9%, 1 rough period, 1 deploy");
  });

  it("on the Pulse, View as table lists every bucket of that app", async () => {
    await openObs("?view=apps");
    const row = $("[data-testid='obs-pulse-row'][data-target='t_live']");
    expect(row).not.toBeNull();
    expect(row!.querySelectorAll("rect.obs-cell")).toHaveLength(96);
    await click(row!.querySelector("[data-testid='obs-pulse-table-toggle']"));
    const rows = $$("[data-testid='obs-pulse-row'][data-target='t_live'] [data-testid='obs-pulse-table'] tbody tr");
    expect(rows).toHaveLength(96);
    expect(rows[40].getAttribute("data-k")).toBe("gap");
    expect(rows[40].textContent).toContain("—");
  });

  it("a target with no probes at all reads No signal over an all-gap strip", async () => {
    await openObs("?view=apps");
    const row = $("[data-testid='obs-pulse-row'][data-target='t_quiet']")!;
    expect(row.querySelector(".inf-state-word")?.textContent).toBe("No signal");
    const cells = [...row.querySelectorAll("rect.obs-cell")];
    expect(cells.length).toBe(96);
    expect(cells.every((c) => c.getAttribute("data-k") === "gap")).toBe(true);
    expect(row.querySelector("[data-testid='obs-pulse-p95']")?.textContent).toBe("no probes");
  });
});
