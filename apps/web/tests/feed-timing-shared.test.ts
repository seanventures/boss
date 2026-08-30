import { afterEach, beforeEach, expect, test } from "vitest";

import { installFeedCache, slowestFeeds } from "../lib/feed-cache";

/**
 * The instrumentation that names a slow feed must actually see the feeds.
 *
 * It did not. Next gives a route handler and a page separate server bundles, so each got its
 * own copy of feed-cache and its own timings Map: the page filled one, /api/render-timing read
 * the other, and the report was empty every time. An empty "slowest feeds" list reads as "no
 * feed is slow" — the most confident possible way to be wrong, and it cost three wrong
 * explanations for the console's load time before the numbers were measured properly.
 */

const API = "https://api.test";
const realFetch = globalThis.fetch;
let delays: Record<string, number>;

beforeEach(() => {
  delays = {};
  const g = globalThis as unknown as {
    __bossFeedCache?: true;
    __bossFeedTimings?: Map<string, unknown>;
  };
  delete g.__bossFeedCache;
  delete g.__bossFeedTimings;

  globalThis.fetch = (async (input: unknown) => {
    const url = String(input);
    const wait = delays[url] ?? 0;
    if (wait) await new Promise((r) => setTimeout(r, wait));
    return { ok: true, status: 200, clone() { return this; } } as unknown as Response;
  }) as typeof fetch;

  // A 1ms reuse window, so two calls to the same path are both really timed. The default 5s
  // would serve the second from cache and there would be nothing to compare.
  process.env.BOSS_FEED_CACHE_MS = "1";
  installFeedCache(API);
});

afterEach(() => {
  globalThis.fetch = realFetch;
  const g = globalThis as unknown as { __bossFeedCache?: true; __bossFeedTimings?: unknown };
  delete g.__bossFeedCache;
  delete g.__bossFeedTimings;
});

test("timings survive across module instances", () => {
  // The other bundle's copy of this module writes to the same process-wide table. If the table
  // lived in module scope instead, this write would be invisible here — which is precisely the
  // bug: /api/render-timing read an empty Map and reported "no slow feeds".
  const g = globalThis as unknown as { __bossFeedTimings?: Map<string, unknown> };
  g.__bossFeedTimings = new Map([["/v1/audit/export", { ms: 13_206, at: Date.now(), count: 50 }]]);
  const seen = slowestFeeds();
  expect(seen.map((f) => f.url)).toContain("/v1/audit/export");
  expect(seen[0].ms).toBe(13_206);
  expect(seen[0].count).toBe(50);
});

test("a control-plane GET is recorded under its path", async () => {
  delays[`${API}/v1/audit/export`] = 40;
  await globalThis.fetch(`${API}/v1/audit/export`);
  const seen = slowestFeeds();
  expect(seen.map((f) => f.url)).toContain("/v1/audit/export");
  expect(seen[0].ms).toBeGreaterThanOrEqual(30);
});

test("the slowest feed sorts first — that is the one holding the barrier", async () => {
  delays[`${API}/v1/slow`] = 60;
  await globalThis.fetch(`${API}/v1/fast`);
  await globalThis.fetch(`${API}/v1/slow`);
  expect(slowestFeeds()[0].url).toBe("/v1/slow");
});

test("the worst time is kept, not the latest", async () => {
  // A 13s call followed by a fast one must not erase the 13s. An average or a last-write would
  // hide the single slow call that holds the Promise.all barrier — and that call IS the page.
  delays[`${API}/v1/spiky`] = 50;
  await globalThis.fetch(`${API}/v1/spiky`);
  await new Promise((r) => setTimeout(r, 5));   // let the 1ms reuse window lapse
  delays[`${API}/v1/spiky`] = 0;
  await globalThis.fetch(`${API}/v1/spiky`);

  const seen = slowestFeeds().find((f) => f.url === "/v1/spiky")!;
  expect(seen.ms).toBeGreaterThanOrEqual(40);
  expect(seen.count).toBe(2);
});

test("how often a feed was asked for is recorded", async () => {
  // "One 13s call" and "forty 13s calls" are different problems with different fixes.
  const g = globalThis as unknown as { __bossFeedTimings: Map<string, { count: number }> };
  await globalThis.fetch(`${API}/v1/counted`);
  expect(g.__bossFeedTimings.get("/v1/counted")!.count).toBe(1);
});

test("a call to somewhere other than the control plane is not recorded", async () => {
  await globalThis.fetch("https://elsewhere.test/thing");
  expect(slowestFeeds().map((f) => f.url)).not.toContain("https://elsewhere.test/thing");
});
