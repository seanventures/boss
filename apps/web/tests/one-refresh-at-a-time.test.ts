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

  it("cancels a discarded refresh and its owed work without recording a landing", () => {
    const time = fakeTime();
    let starts = 0;
    const c = createRefreshCoordinator({ start: () => { starts += 1; }, ...time });
    c.request("now");
    time.run(800);
    c.landed();
    expect(c.lastDurationMs()).toBe(800);

    c.request("now");
    c.request("now");
    time.run(900);
    c.cancel();
    expect(c.busy()).toBe(false);
    expect(c.lastDurationMs()).toBe(800);
    c.landed(); // An idle acknowledgement after discard cannot record a success.
    expect(c.lastDurationMs()).toBe(800);
    time.run(121_000);
    expect(starts).toBe(2);

    c.request("now");
    expect(starts).toBe(3);
    expect(c.busy()).toBe(true);
    time.run(121_400);
    c.landed();
    expect(c.lastDurationMs()).toBe(400);
    expect(starts).toBe(3); // The discarded request's owed work is gone.
  });

  it("cancels a scheduled poll cooldown without starting it later", () => {
    const time = fakeTime();
    let starts = 0;
    const c = createRefreshCoordinator({ start: () => { starts += 1; }, ...time });
    c.request("now");
    time.run(2_000);
    c.landed();
    c.request("poll");
    c.cancel();
    c.cancel();
    time.run(4_000);
    expect(starts).toBe(1);
    expect(c.busy()).toBe(false);
    expect(c.lastDurationMs()).toBe(2_000);
    c.request("now");
    expect(starts).toBe(2);
  });

  it("ignores an already queued ceiling callback from a canceled attempt", () => {
    const time = fakeTime();
    const callbacks: (() => void)[] = [];
    let starts = 0;
    const c = createRefreshCoordinator({
      start: () => { starts += 1; }, ...time,
      setTimer: (fn, ms) => { callbacks.push(fn); return time.setTimer(fn, ms); },
    });
    c.request("now");
    const canceledCeiling = callbacks[0];
    c.cancel();
    c.request("now");
    c.request("now");
    canceledCeiling(); // Clearing a timer cannot retract a callback already dispatched.
    expect(starts).toBe(2);
    expect(c.busy()).toBe(true);
    expect(c.lastDurationMs()).toBe(0);
    time.run(350);
    c.landed();
    expect(starts).toBe(3);
    expect(c.lastDurationMs()).toBe(350);
    c.dispose();
  });

  it("ignores an already queued cooldown callback after cancellation", () => {
    const time = fakeTime();
    const callbacks: (() => void)[] = [];
    let starts = 0;
    const c = createRefreshCoordinator({
      start: () => { starts += 1; }, ...time,
      setTimer: (fn, ms) => { callbacks.push(fn); return time.setTimer(fn, ms); },
    });
    c.request("now");
    time.run(500);
    c.landed();
    c.request("poll");
    const canceledCooldown = callbacks[1];
    c.cancel();
    c.request("now");
    canceledCooldown();
    expect(starts).toBe(2);
    expect(c.busy()).toBe(true);
    c.dispose();
  });

  it("cannot resume disposed work after cancellation or a late acknowledgement", () => {
    const time = fakeTime();
    let starts = 0;
    const c = createRefreshCoordinator({ start: () => { starts += 1; }, ...time });
    c.request("now");
    c.request("now");
    c.cancel();
    c.dispose();
    c.request("now");
    c.landed();
    time.run(130_000);
    expect(starts).toBe(1);
    expect(c.busy()).toBe(false);
    expect(c.lastDurationMs()).toBe(0);
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
      // A ROUTER refresh: `router.refresh()`, or `.refresh()` on any name bound to useRouter().
      // A polled hook's own `.refresh()` (usePolled, the identity mirror) re-reads one feed and
      // never re-renders the console, so it is not one (the hooks came after this rule).
      const code = codeOnly(file);
      const routers = new Set(["router", ...[...code.matchAll(/\b(?:const|let)\s+(\w+)\s*=\s*useRouter\(\)/g)].map((m) => m[1])]);
      const n = [...code.matchAll(/\b(\w+)\.refresh\(\s*\)/g)].filter((m) => routers.has(m[1])).length;
      // The shell's are all handed to the coordinator: its `start` (bare, or inside the
      // transition on a route page without a render token) and its `unstick` (a refresh Next
      // never completed is re-navigated and asked again; lib/console-refresh.ts).
      const allowed = file === "app/ConsoleShell.tsx" ? 3 : file === "lib/console-refresh.ts" ? 1 : 0;
      if (n > allowed) offenders.push(`${file} (${n})`);
    }
    expect(offenders, "ask through useConsoleRefresh() (lib/console-refresh.ts)").toEqual([]);
    const shell = codeOnly("app/ConsoleShell.tsx");
    expect(shell).toMatch(/createRefreshCoordinator\(\{\s*start: tokenDriven\s*\? \(\) => \{ router\.refresh\(\); \}\s*: \(\) => startRefresh\(\(\) => \{ router\.refresh\(\); \}\),/);
    expect(shell).toMatch(/unstick: \(\) => \{\s*router\.replace\(/);
  });

  it("the area loader and its retry ask the coordinator, and a retry waits for it to be idle", () => {
    const shell = codeOnly("app/ConsoleShell.tsx");
    const ensure = shell.slice(shell.indexOf("const ensureLoaded = useCallback"),
                               shell.indexOf("const activeLoading"));
    expect(ensure).toContain('refreshCoordinator.request("now")');
    const retry = shell.slice(shell.indexOf("const areaRetries"), shell.indexOf("const [loadSlow"));
    expect(retry).toContain("refreshCoordinator.busy()");
    // `refreshing`: the coordinator's own in-flight state, not React's transition flag (which
    // settles within a frame of router.refresh() and let a retry overlap the refresh it retried).
    expect(retry).toContain("refreshing");
    expect(shell).not.toContain("startAreaLoad");
  });
});

describe("a refresh that never lands is unstuck, not waited out (2026-10-10)", () => {
  // Next 15.5 sometimes never completes a router.refresh() issued in the first seconds after a
  // load: 7 of 9 first area clicks on the laptop install sat on "Loading Factory…" for 20 s,
  // until the background poll. A navigation drops the stuck action; the shell's `unstick` does one.
  it("unsticks a refresh that has not landed in time, a bounded number of times", () => {
    const time = fakeTime();
    let starts = 0;
    let unsticks = 0;
    const c = createRefreshCoordinator({
      start: () => { starts += 1; }, unstick: () => { unsticks += 1; }, ...time,
      watchdogMs: () => 1_000, maxUnsticks: 2, ceilingMs: 60_000,
    });
    c.request("now");
    time.run(999);
    expect(unsticks).toBe(0);
    time.run(1_000);
    expect(unsticks).toBe(1);
    time.run(10_000);
    expect(unsticks).toBe(2);           // capped
    expect(starts).toBe(1);             // an unstick is not a second refresh in the queue
    c.landed();
    expect(c.busy()).toBe(false);
  });

  it("a refresh that lands in time is never unstuck", () => {
    const time = fakeTime();
    let unsticks = 0;
    const c = createRefreshCoordinator({ start: () => {}, unstick: () => { unsticks += 1; }, ...time });
    c.request("now");
    time.run(300);
    c.landed();
    time.run(60_000);
    expect(unsticks).toBe(0);
  });

  it("an area load is unstuck within a second; a poll is given three times what the last took", () => {
    const time = fakeTime();
    const at: number[] = [];
    const c = createRefreshCoordinator({ start: () => {}, unstick: () => { at.push(time.now()); }, ...time,
                                         maxUnsticks: 1 });
    c.request("now");
    time.run(5_000);
    expect(at[0]).toBeLessThanOrEqual(1_000);
    c.landed();                          // lastDuration = 5 s
    at.length = 0;
    time.run(200_000);
    c.request("poll");
    time.run(260_000);
    expect(at[0] - 200_000).toBe(15_000);
  });

  it("the shell lands a refresh on the server's new render, not on React's transition flag", () => {
    const shell = codeOnly("app/ConsoleShell.tsx");
    expect(shell).toContain("renderToken");
    // rc9 merge: a render that commits with a URL restore is not the canceled refresh landing.
    expect(shell).toMatch(/lastToken\.current = renderToken;\s*if \(!canceled\) refreshCoordinator\.landed\(\);/);
    expect(codeOnly("app/page.tsx")).toMatch(/renderToken=\{/);
  });
});


describe("an area not yet in the document is opened with a page load (2026-10-10)", () => {
  // A router.refresh() that fetched the area into the open page was left uncommitted by Next
  // 15.5 for most first clicks (Command Center to Activity: 4 in 4); a document load of the area
  // took 0.38-0.41 s every time. The refresh is kept only where a page load would lose a draft.
  it("loads the document unless something was typed since it loaded", () => {
    const shell = codeOnly("app/ConsoleShell.tsx");
    const ensure = shell.slice(shell.indexOf("const ensureLoaded = useCallback"),
                               shell.indexOf("const activeLoading"));
    expect(ensure).toMatch(/if \(!typedSinceLoad\.current\) \{[\s\S]*window\.location\.assign\(url\.toString\(\)\);/);
    expect(ensure).toContain('refreshCoordinator.request("now")');
    expect(shell).toMatch(/document\.addEventListener\("input", onInput, true\)/);
  });
});
