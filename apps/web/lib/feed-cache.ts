/**
 * A very short-lived cache and in-flight collapser for control-plane GETs, server side only.
 *
 * ONE console render issues 82 API calls. The auto-refresh, a second tab, and an operator
 * pressing reload put several of those renders in flight at once, so the API sees hundreds of
 * concurrent requests for the same handful of feeds. Measured consequences:
 *
 *   - TTFB of 24s on the overview area and 115s on code_tasks (the server sends nothing until
 *     every feed has answered);
 *   - `/health` unable to get a thread inside its timeout, so Docker restarts the container —
 *     which is what kills in-flight deployments ("the API restarted while this deployment was
 *     running") and what returns 502 to whoever was mid-click.
 *
 * The machine is not the problem: 0 OOM kills, 0.09 load per core, 2.9GB of 257GB used. The
 * problem is request VOLUME, and the cheapest honest reduction is to stop asking the same
 * question many times in the same second.
 *
 * Two mechanisms, both conservative:
 *
 *   COLLAPSE — identical GETs already in flight share one response. This is not a cache; it
 *   cannot serve anything stale, because the single request has not returned yet. It is pure
 *   waste removal and does the heavy lifting when renders overlap.
 *
 *   TTL — a completed GET is reused for a few seconds. This is the part that can serve stale
 *   data, so the window is deliberately small: a control plane where a click's effect appears
 *   within a couple of seconds still feels live, and every mutating path already refetches its
 *   own row rather than relying on a page render.
 *
 * Deliberately NOT cached: anything that is not a GET, anything not addressed to the control
 * plane, and (via BOSS_FEED_CACHE_MS=0) everything, if this ever needs to be switched off
 * without a rebuild.
 */

/** A response read once and kept as bytes: every caller gets its OWN fresh Response.
 *  Cloning one shared Response raced — a joiner cloning while another reader drained it got
 *  "Response.clone: Body has already been consumed", which reached the console as a red 502. */
type Stored = { status: number; statusText: string; headers: [string, string][]; body: ArrayBuffer };
type Entry = { at: number; stored: Stored };

function fresh(stored: Stored): Response {
  // 204/304 cannot carry a body.
  const body = stored.status === 204 || stored.status === 304 ? null : stored.body.slice(0);
  return new Response(body, { status: stored.status, statusText: stored.statusText,
                              headers: stored.headers });
}

async function store(response: Response): Promise<Stored> {
  return { status: response.status, statusText: response.statusText,
           headers: [...response.headers.entries()], body: await response.arrayBuffer() };
}

/** Control-plane reads that are never reused: state a write changes and the operator watches
 *  for (Repo and models re-reads after Replace token, a check or a Try it run). */
const NEVER_REUSED = ["/v1/setup"];

/**
 * Per-URL durations, so a slow feed can be NAMED.
 *
 * The page's 20s is one Promise.all of 79 feeds, and Promise.all waits for its slowest member
 * — so this is not a parallelism problem, it is one or two slow endpoints holding a barrier.
 * Every control-plane GET already passes through here, so this is the one place that can see
 * all of them without touching 173 call sites.
 */
// ON globalThis, NOT IN MODULE SCOPE.
//
// Next gives a route handler and a page separate server bundles, so each gets its own copy of
// this module and its own Map. The page filled one; /api/render-timing read the other, which
// was always empty — the instrumentation looked like it proved the feeds were fast when it was
// simply never seeing them. One process, one table.
type Timing = { ms: number; at: number; count: number };

function timings(): Map<string, Timing> {
  const g = globalThis as unknown as { __bossFeedTimings?: Map<string, Timing> };
  if (!g.__bossFeedTimings) g.__bossFeedTimings = new Map();
  return g.__bossFeedTimings;
}

export function slowestFeeds(limit = 15): Array<{ url: string; ms: number; count: number }> {
  return [...timings().entries()]
    .map(([url, v]) => ({ url, ms: v.ms, count: v.count }))
    .sort((a, b) => b.ms - a.ms)
    .slice(0, limit);
}

const DEFAULT_TTL_MS = 5_000;

function ttlMs(): number {
  const raw = process.env.BOSS_FEED_CACHE_MS;
  if (raw === undefined) return DEFAULT_TTL_MS;
  const n = Number(raw);
  return Number.isFinite(n) && n >= 0 ? n : DEFAULT_TTL_MS;
}

/** Install once per process. Idempotent: a hot reload must not wrap the wrapper. */
export function installFeedCache(apiUrl: string): void {
  if (typeof window !== "undefined") return;          // browser: never
  if (!apiUrl) return;                                 // misconfigured: leave it alone
  const g = globalThis as unknown as { __bossFeedCache?: true; fetch: typeof fetch };
  if (g.__bossFeedCache) return;
  g.__bossFeedCache = true;

  const original = g.fetch.bind(globalThis);
  const inFlight = new Map<string, Promise<Stored>>();
  const completed = new Map<string, Entry>();

  const evict = () => {
    const cutoff = Date.now() - ttlMs();
    for (const [key, entry] of completed) {
      if (entry.at < cutoff) completed.delete(key);
    }
  };

  g.fetch = (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const ttl = ttlMs();
    const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
    const url =
      typeof input === "string" ? input
      : input instanceof URL ? input.toString()
      : input.url;

    // Only idempotent reads of the control plane. A POST that deploys something must never be
    // collapsed into another caller's POST, and a cached DELETE would be a genuine hazard.
    if (ttl === 0 || method !== "GET" || !url.startsWith(apiUrl)
        || NEVER_REUSED.some((path) => url.slice(apiUrl.length).startsWith(path))) {
      return original(input as RequestInfo, init);
    }

    // The Authorization header is identical for every server-side call (one service key), so
    // the URL alone identifies the response. If per-user keys ever arrive this must include
    // them, or one operator would see another's data — hence the explicit note rather than a
    // silent assumption.
    const key = url;

    const hit = completed.get(key);
    if (hit && Date.now() - hit.at < ttl) return fresh(hit.stored);

    const flight = inFlight.get(key);
    if (flight) return fresh(await flight);

    const started = Date.now();
    const promise: Promise<Stored> = original(input as RequestInfo, init)
      .then(async (response) => {
        const path = key.replace(apiUrl, "");
        const table = timings();
        const prev = table.get(path);
        // Keep the WORST time seen and how often it was asked for. An average would hide the
        // one 13-second call inside forty fast ones, and it is the slow one that holds the
        // Promise.all barrier and therefore the page.
        table.set(path, {
          ms: Math.max(prev?.ms ?? 0, Date.now() - started),
          at: Date.now(),
          count: (prev?.count ?? 0) + 1,
        });
        if (table.size > 300) table.clear();
        // Only successful responses are reusable. Caching a 500 for five seconds would turn a
        // blip into an outage that outlives it.
        const stored = await store(response);
        if (response.ok) {
          completed.set(key, { at: Date.now(), stored });
          if (completed.size > 500) evict();
        }
        return stored;
      })
      .finally(() => {
        inFlight.delete(key);
      });

    inFlight.set(key, promise);
    return fresh(await promise);
  }) as typeof fetch;
}
