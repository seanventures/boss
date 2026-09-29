import { afterEach, beforeEach, expect, test, vi } from "vitest";

import {
  __resetRunDeploymentCache,
  forgetRunDeployment,
  loadRunDeployment,
} from "../lib/run-deployments";

/**
 * The batcher exists because of a measurement, and these tests pin that measurement's fix.
 *
 * Loading the Factory area with no interaction at all fired 111 Server Actions — 82 of them
 * the per-card deployment lookup — for 64.5s of action time, and the page was not quiet for
 * 74 seconds. Each action re-rendered the whole route to reply. Forty cards, forty renders.
 *
 * So the property that matters is not "the data is right", it is "forty cards cost ONE
 * request". A regression here is invisible in the UI and costs a minute of render.
 */

type Call = { url: string };
let calls: Call[] = [];
let respond: (url: string) => { status: number; body: unknown };

const realFetch = globalThis.fetch;

beforeEach(() => {
  calls = [];
  respond = () => ({ status: 200, body: { runs: {} } });
  __resetRunDeploymentCache();
  globalThis.fetch = (async (input: unknown) => {
    const url = String(input);
    calls.push({ url });
    const { status, body } = respond(url);
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
    } as Response;
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = realFetch;
  __resetRunDeploymentCache();
  vi.useRealTimers();
});

const ids = (url: string) =>
  decodeURIComponent(new URL(url, "http://x").searchParams.get("run_ids") ?? "")
    .split(",")
    .filter(Boolean);

// --------------------------------------------------------------------------- //
// The reason this module exists
// --------------------------------------------------------------------------- //
test("forty cards mounting together cost one request, not forty", async () => {
  respond = (url) => ({
    status: 200,
    body: { runs: Object.fromEntries(ids(url).map((i) => [i, { deployments: [] }])) },
  });

  const runs = Array.from({ length: 40 }, (_, i) => `crun_${i}`);
  const results = await Promise.all(runs.map((r) => loadRunDeployment(r, "App")));

  expect(calls.length, "forty mounts must collapse into a single fetch").toBe(1);
  expect(ids(calls[0].url).length).toBe(40);
  expect(results.length).toBe(40);
  expect(results.every((r) => !r.error)).toBe(true);
});

test("each card gets its own answer, not the first one", async () => {
  respond = () => ({
    status: 200,
    body: {
      runs: {
        a: { linked_host: "host-a" },
        b: { linked_host: "host-b" },
      },
    },
  });
  const [a, b] = await Promise.all([loadRunDeployment("a"), loadRunDeployment("b")]);
  expect(a.linked_host).toBe("host-a");
  expect(b.linked_host).toBe("host-b");
});

test("app names ride along positionally with their run ids", async () => {
  // Several apps share one repo, and without the name the upstream endpoint once reported a
  // deployment for a different app entirely. A batch must not scramble that pairing.
  respond = () => ({ status: 200, body: { runs: {} } });
  await Promise.all([
    loadRunDeployment("r1", "Costs, Q3"),
    loadRunDeployment("r2", "Living Colors"),
  ]);
  const url = new URL(calls[0].url, "http://x");
  expect(ids(calls[0].url)).toEqual(["r1", "r2"]);
  expect(url.searchParams.getAll("app_name")).toEqual(["Costs, Q3", "Living Colors"]);
});

test("a batch larger than the handler's cap is chunked, never truncated", async () => {
  // Truncating would silently report "not deployed" for the dropped runs.
  respond = (url) => ({
    status: 200,
    body: { runs: Object.fromEntries(ids(url).map((i) => [i, { deployments: [] }])) },
  });
  const runs = Array.from({ length: 130 }, (_, i) => `crun_${i}`);
  const results = await Promise.all(runs.map((r) => loadRunDeployment(r)));

  expect(calls.length, "130 ids at 60 per request").toBe(3);
  expect(calls.flatMap((c) => ids(c.url)).length).toBe(130);
  expect(results.every((r) => !r.error), "no card may be dropped by chunking").toBe(true);
});

test("six hundred cards never launch all ten route chunks at once", async () => {
  let active = 0;
  let peak = 0;
  globalThis.fetch = (async (input: unknown) => {
    calls.push({ url: String(input) });
    active += 1;
    peak = Math.max(peak, active);
    await new Promise((resolve) => setTimeout(resolve, 1));
    active -= 1;
    return {
      ok: true,
      status: 200,
      json: async () => ({
        runs: Object.fromEntries(ids(String(input)).map((id) => [id, { deployments: [] }])),
      }),
    } as Response;
  }) as typeof fetch;
  const results = await Promise.all(
    Array.from({ length: 600 }, (_, i) => loadRunDeployment(`run-${i}`)),
  );
  expect(calls).toHaveLength(10);
  expect(peak).toBe(2); // At most eight upstream API reads: two routes × four each.
  expect(results.every((r) => !r.error)).toBe(true);
});

test("queued route chunks expire explicitly when earlier chunks stall", async () => {
  vi.useFakeTimers();
  globalThis.fetch = ((input: unknown) => {
    calls.push({ url: String(input) });
    return new Promise<Response>(() => {});
  }) as typeof fetch;
  const resultsPromise = Promise.all(
    Array.from({ length: 180 }, (_, i) => loadRunDeployment(`run-${i}`)),
  );
  await vi.advanceTimersByTimeAsync(20_016);
  const results = await resultsPromise;
  expect(calls).toHaveLength(2);
  expect(results).toHaveLength(180);
  expect(results.every((r) => r.error)).toBe(true);
  expect(results[179].error).toMatch(/timed out before request/);
});

// --------------------------------------------------------------------------- //
// Absence vs. silence — different facts, opposite conclusions
// --------------------------------------------------------------------------- //
test("a run the server did not mention reports an error, not 'no deployments'", async () => {
  // Conflating these is what once showed a deployed app as never deployed, beside an enabled
  // Deploy button that would have shipped it a second time.
  respond = () => ({ status: 200, body: { runs: {} } });
  const r = await loadRunDeployment("ghost");
  expect(Boolean(r.error), "silence must be reported as silence").toBe(true);
  expect(r.deployments).toBe(undefined);
});

test("a failed request resolves with an error rather than rejecting", async () => {
  // A rejection a caller forgot to catch would leave the card asserting absence forever.
  respond = () => ({ status: 500, body: {} });
  const r = await loadRunDeployment("r1");
  expect(String(r.error)).toMatch(/500/);
});

test("one failed batch does not leave the other cards hanging", async () => {
  respond = () => ({ status: 502, body: {} });
  const results = await Promise.all(
    Array.from({ length: 10 }, (_, i) => loadRunDeployment(`r${i}`)),
  );
  expect(results.length).toBe(10);
  expect(results.every((r) => r.error), "every waiter must be settled").toBe(true);
});

test("a missing run id is refused without a request", async () => {
  const r = await loadRunDeployment("   ");
  expect(String(r.error)).toMatch(/missing run id/);
  expect(calls.length).toBe(0);
});

// --------------------------------------------------------------------------- //
// Caching must not outlive the operator's own action
// --------------------------------------------------------------------------- //
test("a repeat ask inside the window reuses the answer", async () => {
  respond = () => ({ status: 200, body: { runs: { r1: { linked_host: "h" } } } });
  await loadRunDeployment("r1");
  await loadRunDeployment("r1");
  expect(calls.length).toBe(1);
});

test("forgetting a run forces the next ask to be a real one", async () => {
  // After Deploy / Unbind / Retry the card must not render the pre-action world.
  respond = () => ({ status: 200, body: { runs: { r1: { linked_host: "h" } } } });
  await loadRunDeployment("r1");
  forgetRunDeployment("r1");
  await loadRunDeployment("r1");
  expect(calls.length).toBe(2);
});

test("forgetting one run does not evict the other thirty-nine", async () => {
  respond = () => ({ status: 200, body: { runs: { r1: {}, r2: {} } } });
  await Promise.all([loadRunDeployment("r1"), loadRunDeployment("r2")]);
  expect(calls.length).toBe(1);
  forgetRunDeployment("r1");
  await Promise.all([loadRunDeployment("r1"), loadRunDeployment("r2")]);
  expect(calls.length, "only the forgotten run is re-fetched").toBe(2);
});

test("forgetting clears every app-name variant of a run", async () => {
  respond = () => ({ status: 200, body: { runs: { r1: {} } } });
  await loadRunDeployment("r1", "Shorty");
  forgetRunDeployment("r1");
  await loadRunDeployment("r1", "Shorty");
  expect(calls.length).toBe(2);
});
