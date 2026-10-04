/**
 * Every console refresh goes through one coordinator: at most one in flight, at most one owed.
 *
 * boss-1, 2026-10-04: opening Discovery showed "Loading Discovery… taking longer than usual"
 * and never the area, while the API log showed the server rendering Discovery four times,
 * 16-48 s apart. Next.js chains refreshes (one asked for mid-flight queues behind it), and React
 * renders every pending transition together, waiting on the NEWEST refresh — so while panels on
 * screen asked for a refresh every 4-5 s against a render that took 16-48 s, nothing committed.
 */
import { readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { createRefreshCoordinator, requestConsoleRefresh, registerConsoleRefresh }
  from "../lib/console-refresh";
import { codeOnly } from "./_helpers/source";

/** A fake clock and timer queue, so the coordinator runs for real, in order. */
function fakeTime() {
  let t = 0;
  let seq = 0;
  const timers = new Map<number, { at: number; fn: () => void }>();
  return {
    now: () => t,
    setTimer: (fn: () => void, ms: number) => { const id = ++seq; timers.set(id, { at: t + ms, fn }); return id; },
    clearTimer: (id: unknown) => { timers.delete(id as number); },
    /** Advance to `until`, firing every timer due on the way, in time order. */
    run(until: number) {
      for (;;) {
        let next: [number, { at: number; fn: () => void }] | null = null;
        for (const e of timers) if (e[1].at <= until && (!next || e[1].at < next[1].at)) next = e;
        if (!next) break;
        timers.delete(next[0]);
        t = next[1].at;
        next[1].fn();
      }
      t = until;
    },
  };
}

describe("the coordinator", () => {
  it("never starts a refresh while one is in flight, and owes exactly one", () => {
    const time = fakeTime();
    let starts = 0;
    const c = createRefreshCoordinator({ start: () => { starts += 1; }, ...time });
    c.request("now");
    expect(starts).toBe(1);
    for (let i = 0; i < 10; i++) c.request(i % 2 ? "poll" : "now");
    expect(starts).toBe(1);
    expect(c.busy()).toBe(true);
    time.run(5_000);
    c.landed();
    // The owed one starts the moment the first lands — it sees everything asked meanwhile.
    expect(starts).toBe(2);
    c.landed();
    expect(starts).toBe(2);
    expect(c.busy()).toBe(false);
  });

  it("a poll waits out a cool-down as long as the last refresh took; `now` does not", () => {
    const time = fakeTime();
    let starts = 0;
    const c = createRefreshCoordinator({ start: () => { starts += 1; }, ...time });
    c.request("now");
    time.run(20_000);
    c.landed();
    expect(c.lastDurationMs()).toBe(20_000);
    c.request("poll");
    expect(starts).toBe(1);
    time.run(39_999);
    expect(starts).toBe(1);
    time.run(40_000);
    expect(starts).toBe(2);
    time.run(41_000);
    c.landed();
    c.request("now");
    expect(starts).toBe(3);
  });

  it("a refresh whose landing is never reported cannot stop the console refreshing", () => {
    const time = fakeTime();
    let starts = 0;
    const c = createRefreshCoordinator({ start: () => { starts += 1; }, ...time, ceilingMs: 90_000 });
    c.request("now");
    c.request("now");
    time.run(90_000);
    expect(starts).toBe(2);
  });

  it("with no shell mounted, a request refreshes directly", () => {
    let direct = 0;
    requestConsoleRefresh({ refresh: () => { direct += 1; } }, "now");
    expect(direct).toBe(1);
    let coordinated = 0;
    const c = createRefreshCoordinator({ start: () => { coordinated += 1; } });
    const off = registerConsoleRefresh(c);
    requestConsoleRefresh({ refresh: () => { direct += 1; } }, "now");
    off();
    c.dispose();
    expect([direct, coordinated]).toEqual([1, 1]);
  });
});

/**
 * The browser, modelled: Next.js runs refreshes one after another, each taking `renderMs`, and
 * React commits only when every refresh asked for so far has landed (all pending transitions
 * render together and the router's state is the newest refresh's promise). The area is on
 * screen once a refresh that STARTED after the area was asked for commits.
 */
function simulate(opts: { coordinated: boolean; pollEveryMs: number; renderMs: number; horizonMs: number }) {
  const time = fakeTime();
  const queue: number[] = [];       // start times of refreshes not yet landed, in order
  let runningUntil = -1;
  let areaAskedAt = -1;
  let committedAt = -1;
  let refreshes = 0;
  let coordinator: ReturnType<typeof createRefreshCoordinator> | null = null;

  const nextRefresh = () => {
    if (runningUntil >= 0 || queue.length === 0) return;
    runningUntil = time.now() + opts.renderMs;
    time.setTimer(() => {
      const started = queue.shift()!;
      runningUntil = -1;
      refreshes += 1;
      if (queue.length === 0) {
        // Nothing newer is pending: React commits.
        if (committedAt < 0 && areaAskedAt >= 0 && started >= areaAskedAt) committedAt = time.now();
        coordinator?.landed();
      }
      nextRefresh();
    }, opts.renderMs);
  };
  const routerRefresh = () => { queue.push(time.now()); nextRefresh(); };
  if (opts.coordinated) coordinator = createRefreshCoordinator({ start: routerRefresh, ...time });
  const ask = (kind: "poll" | "now") => (coordinator ? coordinator.request(kind) : routerRefresh());

  // A panel already on screen polls (a live code task: 4 s).
  const poll = () => { ask("poll"); time.setTimer(poll, opts.pollEveryMs); };
  time.setTimer(poll, opts.pollEveryMs);
  // A little later the operator opens Discovery.
  time.setTimer(() => { areaAskedAt = time.now(); ask("now"); }, 10_000);
  time.run(opts.horizonMs);
  return { committedAt, waitedMs: committedAt < 0 ? Infinity : committedAt - areaAskedAt, refreshes };
}

describe("an area opened while panels poll", () => {
  const boss1 = { pollEveryMs: 4_000, renderMs: 20_000, horizonMs: 300_000 };

  it("bare router.refresh() polling never lets the area commit (the boss-1 report)", () => {
    const bare = simulate({ ...boss1, coordinated: false });
    expect(bare.committedAt).toBe(-1);
    expect(bare.refreshes).toBeGreaterThan(10);  // the server kept rendering, nothing showed
  });

  it("through the coordinator it commits within two renders, however often panels ask", () => {
    const run = simulate({ ...boss1, coordinated: true });
    expect(run.waitedMs).toBeLessThanOrEqual(2 * boss1.renderMs);
    for (const renderMs of [2_000, 16_000, 48_000]) {
      expect(simulate({ ...boss1, renderMs, coordinated: true }).waitedMs)
        .toBeLessThanOrEqual(2 * renderMs);
    }
  });
});

/** Every .ts/.tsx under a directory. */
function sources(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) out.push(...sources(path));
    else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(path);
  }
  return out;
}

describe("nothing refreshes the console behind the coordinator's back", () => {
  it("the only router.refresh() calls are the coordinator's", () => {
    const offenders: string[] = [];
    for (const file of [...sources("app"), ...sources("lib"), ...sources("components")]) {
      const n = (codeOnly(file).match(/\.refresh\(\s*\)/g) ?? []).length;
      const allowed = file === "lib/console-refresh.ts" || file === "app/ConsoleShell.tsx" ? 1 : 0;
      if (n > allowed) offenders.push(`${file} (${n})`);
    }
    expect(offenders, "ask through useConsoleRefresh() (lib/console-refresh.ts)").toEqual([]);
    const shell = codeOnly("app/ConsoleShell.tsx");
    expect(shell).toMatch(/createRefreshCoordinator\(\{\s*start: \(\) => startRefresh\(\(\) => \{ router\.refresh\(\); \}\)/);
  });

  it("the area loader and its retry ask the coordinator, and a retry waits for it to be idle", () => {
    const shell = codeOnly("app/ConsoleShell.tsx");
    const ensure = shell.slice(shell.indexOf("const ensureLoaded = useCallback"),
                               shell.indexOf("const activeLoading"));
    expect(ensure).toContain('refreshCoordinator.request("now")');
    const retry = shell.slice(shell.indexOf("const areaRetries"), shell.indexOf("const [loadSlow"));
    expect(retry).toContain("refreshCoordinator.busy()");
    expect(retry).toContain("refreshPending");
    expect(shell).not.toContain("startAreaLoad");
  });
});
