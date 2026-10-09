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
 *
 * A PERSON'S ANSWER IS NEVER REUSED AFTER IT RETURNED (round 3, R3-STACK S2). The TTL entry for a
 * session was the API's answer to a request the API had authorized BEFORE — so for five seconds
 * after a revocation (SCIM deactivating someone, "Sign out everywhere", an admin disabling a
 * person, a role taken away) the console kept answering that person's reads, /v1/identity/me
 * included, with 200 and data, while the API itself already said 401. The TTL now applies only
 * to requests that carry no session; a person's reads are collapsed while in flight (one
 * request's answer, never an older one) and asked afresh after. And the browser's OWN calls
 * through the /v1 proxy (NO_REUSE_HEADER) are never collapsed or kept at all: a request the
 * browser sends after a revocation is decided by the API, every time.
 *
 * …UNLESS THIS REQUEST HAS PROVEN THE SESSION LIVE (console-latency fix, 2026-10-09). Without any
 * reuse, every render — a page load, an area opened, each background refresh — asked the API all
 * of its ~40 feeds again, each one paying the identity guard, and rc6's Factory took 3.6-4.6 s on
 * boss-1 where rc5 (which kept everything for 5 s, revoked or not) was fast. A render now reads
 * /v1/identity/me and /me/permissions FIRST, fresh, never from here (identity paths are never
 * kept); only when both answered does the page call enterLiveProof(), and only for the rest of
 * THAT request may a kept answer for the same session be served — and only one fetched at the
 * same identity epoch (/me/permissions says which), within the TTL. So:
 *   - a revoked session, a disabled person: the next request's own /me fails, the page signs in
 *     and asks for no feed — nothing kept is ever served to it;
 *   - a role or binding change bumps the epoch: the next request's proof names the new epoch,
 *     and nothing kept under the old one matches;
 *   - a request that proved nothing (a route handler, a server action, the browser's own call)
 *     reuses nothing, exactly as before.
 */

/**
 * THE BROWSER'S OWN CALL IS MARKED ON THE RequestInit, NOT IN A HEADER (id-sec-final2 B1). A header
 * marker never arrived: the identity wrapper (server-fetch.ts), installed OUTSIDE this cache,
 * strips every `x-boss-*` header before the cache sees the request, so `direct` was always false
 * and the browser's sessionless calls were TTL-kept and shared. A symbol property on the init
 * survives the wrapper's `{ ...init, headers }` (and server-fetch.ts carries it explicitly onto a
 * Request), and no client can send one. `Symbol.for`: Next gives route handlers and pages separate
 * copies of this module, and both must recognise the same mark.
 */
export const NO_REUSE = Symbol.for("boss.feed-cache.no-reuse");

type Marked = { [NO_REUSE]?: true };

/** Mark a fetch init as the browser's own call: never collapsed, never kept. */
export function noReuse<T extends RequestInit>(init: T): T {
  (init as T & Marked)[NO_REUSE] = true;
  return init;
}

export function isNoReuse(init: RequestInit | undefined | null): boolean {
  return Boolean(init && (init as RequestInit & Marked)[NO_REUSE]);
}

/** What this request proved: whose session answered /me live, and at which identity epoch. */
export type LiveProof = { tag: string; epoch: number };

type ProofStorage = { getStore(): LiveProof | undefined; enterWith(store: LiveProof): void };

/** One store per process (Next gives the page and route handlers separate copies of this module).
 *  Next's server installs globalThis.AsyncLocalStorage; without it (a browser, an odd runtime)
 *  there is no proof and so no reuse — the safe default. */
function proofStorage(): ProofStorage | null {
  const g = globalThis as unknown as {
    __bossLiveProof?: ProofStorage;
    AsyncLocalStorage?: new () => ProofStorage;
  };
  if (g.__bossLiveProof) return g.__bossLiveProof;
  if (typeof window !== "undefined" || typeof g.AsyncLocalStorage !== "function") return null;
  g.__bossLiveProof = new g.AsyncLocalStorage();
  return g.__bossLiveProof;
}

/** The proof for a session cookie the API just answered /me and /me/permissions for, at `epoch`. */
export async function liveProofFor(cookieHeader: string | null | undefined,
                                   epoch: number | null | undefined): Promise<LiveProof | null> {
  const token = parseIdentityCookies(cookieHeader)[SESSION_COOKIE];
  if (!token || typeof epoch !== "number" || !Number.isFinite(epoch)) return null;
  return { tag: await tokenTag(token), epoch };
}

/**
 * For the REST OF THE CURRENT REQUEST (this async context and everything it starts), kept answers
 * for the proven session at the proven epoch may be served. Call it synchronously in the page's
 * own body, after its fresh identity reads succeeded — an `await` between the proof and this call
 * is fine, a helper that calls it after its own await is not (AsyncLocalStorage.enterWith only
 * reaches the caller's continuation when called in the caller's frame).
 */
export function enterLiveProof(proof: LiveProof | null): void {
  if (!proof) return;
  proofStorage()?.enterWith(proof);
}

function currentProof(): LiveProof | undefined {
  return proofStorage()?.getStore();
}

import { consoleAuthMarker, parseIdentityCookies, PREAUTH_COOKIE, SESSION_COOKIE, sessionTag, tokenTag } from "./identity/forward";

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

/** Never kept for a person, even under a proof: the reads that ARE the proof (who is signed in,
 *  what they hold), and every other identity read (sessions, keys, people, roles). */
const NEVER_KEPT_FOR_A_PERSON = /^\/v1\/(identity|auth)(\/|\?|$)/;

/** A writing GET's POST twin (identity_guard.writing_get: `<path>/load`): the console reads with
 *  it, and its only writes are housekeeping (seed a default, reap an expired reservation). It is
 *  treated as the read it replaces — collapsed and kept like a GET — and it does not throw away
 *  every kept read the way a real write does. Only with no body: a twin never takes one. */
function isReadTwin(method: string, path: string, input: RequestInfo | URL,
                    init: RequestInit | undefined): boolean {
  // The body can ride on the init OR on a Request (the identity wrapper outside this cache hands
  // a Request input on as a new Request, with no init): either one makes it a write (rc6.1).
  const requestBody = input instanceof Request ? input.body : null;
  return method === "POST" && /\/load$/.test(path.split("?")[0])
    && (init?.body === undefined || init?.body === null) && requestBody === null;
}

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
  const g = globalThis as unknown as { __bossFeedCache?: true; __bossOriginalFetch?: typeof fetch; fetch: typeof fetch };
  if (g.__bossFeedCache) return;
  g.__bossFeedCache = true;

  const original = g.fetch.bind(globalThis);
  // The platform fetch, for the protocol proxies that must bypass every wrapper (SAML/SCIM:
  // lib/identity/protocol-proxy.ts directFetch).
  g.__bossOriginalFetch ??= original;
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

    // The browser's own call through the /v1 proxy: never collapsed or kept (B1: the mark rides
    // on the init, which the identity wrapper outside this cache keeps).
    const given = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
    const direct = isNoReuse(init);

    // A WRITE MAKES EVERY KEPT READ STALE. The browser's own re-read after a press (give a
    // skill, then read the agent back) comes through a proxy in this process, so it hit the
    // TTL entry from just before the press and the page showed the old state: the chip it had
    // just removed stayed, the server it had just given did not appear (agent Loadout walk).
    // Which reads a write changes is not knowable here, so every write drops them all, before
    // it is sent and again when it lands (a read cached while it was in flight is stale too).
    const path = url.startsWith(apiUrl) ? url.slice(apiUrl.length) : url;
    const readTwin = url.startsWith(apiUrl) && isReadTwin(method, path, input, init);
    if (method !== "GET" && method !== "HEAD" && url.startsWith(apiUrl) && !readTwin) {
      completed.clear();
      try {
        return await original(input as RequestInfo, init);
      } finally {
        completed.clear();
      }
    }

    // Only idempotent reads of the control plane. A POST that deploys something must never be
    // collapsed into another caller's POST, and a cached DELETE would be a genuine hazard.
    if (direct || ttl === 0 || (method !== "GET" && !readTwin) || !url.startsWith(apiUrl)
        || NEVER_REUSED.some((prefix) => path.startsWith(prefix))) {
      return original(input as RequestInfo, init);
    }

    // AN IDENTITY READ IS NEVER SHARED FOR A PERSON, NOT EVEN IN FLIGHT (rc6.1). /me and
    // /me/permissions ARE the live proof: a render that joined another render's in-flight /me
    // would hold an answer the API gave BEFORE this render began — a session revoked, or a role
    // taken away, in between would still "prove" live, at the old epoch, and unlock answers kept
    // for seconds (the-live-proof-cannot-be-borrowed.test.ts). Each render asks for itself.
    const asker = await sessionTag(given);
    if (asker !== "anon" && NEVER_KEPT_FOR_A_PERSON.test(path)) {
      return original(input as RequestInfo, init);
    }

    // ONE PERSON'S ANSWER IS NEVER ANOTHER'S. Every call carries the signed-in person's own
    // session now (lib/identity/server-fetch.ts), and the same URL answers differently for an
    // Owner and a Viewer — so the key is the URL AND whose session asked. A request with no
    // session shares only with other sessionless requests.
    // B2: A SESSIONLESS ANSWER IS ONE CLIENT'S, TOO. The API judges a request without a session
    // by WHERE it comes from (the network bridge admits only a private peer; /v1/auth/options
    // answers about the caller's address) and by its pre-auth cookie (an SSO start). So a
    // sessionless key also names the client hop the identity wrapper forwarded and the pre-auth
    // cookie's tag: two visitors never share one, in flight or kept.
    const preauth = parseIdentityCookies(given.get("cookie"))[PREAUTH_COOKIE];
    const client = asker === "anon"
      ? `\u0000${given.get("x-forwarded-for") ?? "-"}\u0000${preauth ? await tokenTag(preauth) : "-"}`
      : "";
    // A person's answer is kept only under THIS request's proof that the session is live, and
    // only for the same session at the same epoch (see the header). The epoch is in the key, so
    // an answer fetched before a role change can never match a request proven after it.
    const proof = asker !== "anon" ? currentProof() : undefined;
    const proven = !!proof && proof.tag === asker
      && !NEVER_KEPT_FOR_A_PERSON.test(path);
    const key = `${method} ${url}\u0000${asker}${client}${proven ? `\u0000e${proof!.epoch}` : ""}`;
    // A sessionless read uses the TTL — never on a network-trusted console, where every visitor
    // is sessionless and the API's answer depends on who is asking (B2).
    const keep = proven || (asker === "anon" && consoleAuthMarker() === null);

    const hit = keep ? completed.get(key) : undefined;
    if (hit && Date.now() - hit.at < ttl) return fresh(hit.stored);

    const flight = inFlight.get(key);
    if (flight) return fresh(await flight);

    const started = Date.now();
    const promise: Promise<Stored> = original(input as RequestInfo, init)
      .then(async (response) => {
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
        // The API's own word wins: an answer it marks no-store (/v1/auth/options, the setup
        // status) is never kept (B2).
        const noStore = /(^|,)\s*(no-store|private)\s*(,|$)/i.test(response.headers.get("cache-control") ?? "");
        if (response.ok && keep && !noStore) {
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
