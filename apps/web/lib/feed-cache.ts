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

type Entry = { at: number; response: Response };

/**
 * Per-URL durations, so a slow feed can be NAMED.
 *
 * The page's 20s is one Promise.all of 79 feeds, and Promise.all waits for its slowest member
 * — so this is not a parallelism problem, it is one or two slow endpoints holding a barrier.
 * Every control-plane GET already passes through here, so this is the one place that can see
 * all of them without touching 173 call sites.
 */
const timings = new Map<string, { ms: number; at: number }>();

export function slowestFeeds(limit = 15): Array<{ url: string; ms: number }> {
  return [...timings.entries()]
    .map(([url, v]) => ({ url, ms: v.ms }))
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
  const inFlight = new Map<string, Promise<Response>>();
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
    if (ttl === 0 || method !== "GET" || !url.startsWith(apiUrl)) {
      return original(input as RequestInfo, init);
    }

    // The Authorization header is identical for every server-side call (one service key), so
    // the URL alone identifies the response. If per-user keys ever arrive this must include
    // them, or one operator would see another's data — hence the explicit note rather than a
    // silent assumption.
    const key = url;

    const hit = completed.get(key);
    if (hit && Date.now() - hit.at < ttl) return hit.response.clone();

    const flight = inFlight.get(key);
    if (flight) return (await flight).clone();

    const started = Date.now();
    const promise = original(input as RequestInfo, init)
      .then((response) => {
        timings.set(key.replace(apiUrl, ""), { ms: Date.now() - started, at: Date.now() });
        if (timings.size > 300) timings.clear();
        // Only successful responses are reusable. Caching a 500 for five seconds would turn a
        // blip into an outage that outlives it.
        if (response.ok) {
          completed.set(key, { at: Date.now(), response: response.clone() });
          if (completed.size > 500) evict();
        }
        return response;
      })
      .finally(() => {
        inFlight.delete(key);
      });

    inFlight.set(key, promise);
    return (await promise).clone();
  }) as typeof fetch;
}
