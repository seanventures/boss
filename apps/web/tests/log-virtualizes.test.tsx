// @vitest-environment happy-dom
/**
 * The list is windowed (spec §4.2.6, §8.5): 5,000 lines put at most a few hundred rows in the
 * DOM at any scroll depth, repeated lines fold into `×n`, and By run groups under a header that
 * says its counts are of the loaded lines.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { mapActivityLogItem } from "../lib/activity-log";
import { flatten, fold, windowOf } from "../app/activity/log-model";
import { row, rows } from "./_helpers/log-fixtures";
import { $, $$, click, mount, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { LogRows } = await import("../app/activity/log-rows");

const items = (api: Record<string, unknown>[]) => api.flatMap(mapActivityLogItem);
afterEach(() => unmount());

describe("only what is on screen is in the page", () => {
  it("5,000 lines: never more than 300 rows in the DOM, at the top or deep down", async () => {
    const many = items(rows(5000, (i) => ({ title: `line ${i}` })));
    await mount(<LogRows items={many} labels={{}} byRun={false} selectedId={null} arrived={new Set()}
                         onOpen={() => {}} onHolding={() => {}} onNearEnd={() => {}} footer={null} />);
    expect($$("[data-testid=lx-row]").length).toBeLessThanOrEqual(300);
    expect($$("[data-testid=lx-row]").length).toBeGreaterThan(10);
    const list = $("[data-testid=lx-list]")!;
    await act(async () => {
      list.scrollTop = 32 * 4000;
      list.dispatchEvent(new Event("scroll"));
    });
    const deep = $$("[data-testid=lx-row]");
    expect(deep.length).toBeLessThanOrEqual(300);
    expect(deep.some((r) => r.getAttribute("data-id") === "r4000")).toBe(true);
    expect(deep.some((r) => r.getAttribute("data-id") === "r0")).toBe(false);
  });

  it("the window is a binary search over offsets, whatever the depth", () => {
    const flat = flatten(items(rows(10_000)), { byRun: false, open: new Set(), labels: {} });
    const seen = windowOf(flat, 32 * 9000, 900);
    expect(seen.length).toBeLessThan(100);
    expect(seen[0].top).toBeGreaterThan(32 * 8900);
  });
});

describe("repeats fold, runs group", () => {
  it("identical lines within a minute fold into ×n, which opens in place", async () => {
    const same = items([row("a1", { title: "Retrying", created_at: "2026-10-04T10:00:10Z" }),
                        row("a2", { title: "Retrying", created_at: "2026-10-04T10:00:05Z" }),
                        row("a3", { title: "Retrying", created_at: "2026-10-04T10:00:00Z" }),
                        row("b1", { title: "Done", created_at: "2026-10-04T09:59:00Z" })]);
    expect(fold(same).map((f) => f.items.length)).toEqual([3, 1]);
    await mount(<LogRows items={same} labels={{}} byRun={false} selectedId={null} arrived={new Set()}
                         onOpen={() => {}} onHolding={() => {}} onNearEnd={() => {}} footer={null} />);
    const pill = $("[data-testid=lx-fold]")!;
    expect(pill.textContent).toBe("×3");
    expect(pill.getAttribute("title")).toBe("3 identical lines folded");
    expect($$("[data-testid=lx-row]")).toHaveLength(2);
    await click(pill);
    expect($$("[data-testid=lx-row]")).toHaveLength(4);
  });

  it("By run puts a header over each run, counted on the loaded lines", async () => {
    const mixed = items([row("x1", { workflow_run_id: "wfr_1", level: "error", created_at: "2026-10-04T10:00:03Z" }),
                         row("x2", { workflow_run_id: "wfr_1", title: "other", created_at: "2026-10-04T10:00:02Z" }),
                         row("y1", { run_id: "crun_2", created_at: "2026-10-04T10:00:01Z" })]);
    await mount(<LogRows items={mixed} labels={{ wfr_1: "Ship it #182" }} byRun selectedId={null} arrived={new Set()}
                         onOpen={() => {}} onHolding={() => {}} onNearEnd={() => {}} footer={null} />);
    const heads = $$(".lx-runhead");
    expect(heads.map((h) => h.textContent)).toEqual(["Ship it #1822 lines · 1 error", "crun_21 line"]);
    expect(heads[0].getAttribute("title")).toBe("Counted on loaded lines");
  });
});
