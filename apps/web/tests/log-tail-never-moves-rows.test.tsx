// @vitest-environment happy-dom
/**
 * The live tail (spec §4.2.9): every 5s, `after=` the newest line with its ETag — a 304 when
 * nothing happened. At the top with no line open, new lines are inserted (and fade once);
 * otherwise they wait behind `n new ↑` and nothing moves under the reader. Paused and a hidden
 * tab ask nothing at all.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { fakeLogApi, newLog, row, rows, type FakeLog } from "./_helpers/log-fixtures";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { LogExplorer, LOG_POLL_MS } = await import("../app/activity/log-explorer");

let log: FakeLog;
const tails = () => log.asked.filter((u) => u.startsWith("/api/activity?") && u.includes("after="));
const ids = () => $$("[data-testid=lx-row]").map((r) => r.getAttribute("data-id"));

async function tick() {
  await act(async () => { vi.advanceTimersByTime(LOG_POLL_MS); });
  await settle();
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  log = newLog({ page: { items: rows(3), newest_cursor: "2026-10-04T10:00:00Z|r0" } });
  vi.stubGlobal("fetch", fakeLogApi(log));
  setConsoleLive(true);
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
  window.history.replaceState({}, "", "/?area=activity");
});
afterEach(() => { unmount(); vi.useRealTimers(); vi.unstubAllGlobals(); setConsoleLive(true); });

describe("nothing moves under the reader", () => {
  it("polls after= the newest line, and a quiet log answers 304", async () => {
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    await tick();
    expect(tails()).toHaveLength(1);
    expect(tails()[0]).toContain("after=2026-10-04T10%3A00%3A00Z%7Cr0");
    await tick();
    expect(tails()).toHaveLength(2);
    expect(ids()).toEqual(["r0", "r1", "r2"]);
  });

  it("at the top, a new line is inserted and marked fresh", async () => {
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    log.tail.push(row("n1", { created_at: "2026-10-04T10:00:05Z" }));
    await tick();
    expect(ids()).toEqual(["n1", "r0", "r1", "r2"]);
    expect($("[data-id=n1]")!.getAttribute("data-fresh")).toBe("true");
    expect($("[data-testid=lx-new]")).toBeNull();
  });

  it("with a line open, new lines wait behind `n new ↑` and the list does not shift", async () => {
    window.history.replaceState({}, "", "/?area=activity&row=r1");
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    expect($("[data-testid=lx-tail]")!.textContent).toBe("Holding");
    log.tail.push(row("n1", { created_at: "2026-10-04T10:00:05Z" }), row("n2", { created_at: "2026-10-04T10:00:04Z" }));
    await tick();
    expect(ids()).toEqual(["r0", "r1", "r2"]);
    expect($("[data-testid=lx-new]")!.textContent).toBe("2 new ↑");
    await click($("[data-testid=lx-new]"));
    expect(ids()).toEqual(["n1", "n2", "r0", "r1", "r2"]);
  });

  it("Paused asks nothing", async () => {
    setConsoleLive(false);
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    await tick();
    await tick();
    expect(tails()).toHaveLength(0);
  });

  it("a hidden tab asks nothing", async () => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    await tick();
    expect(tails()).toHaveLength(0);
  });
});
