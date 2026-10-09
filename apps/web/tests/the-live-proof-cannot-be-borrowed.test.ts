/**
 * THE LIVE PROOF CANNOT BE BORROWED (rc6.1 security verification of the console-latency fix).
 *
 * The console may serve a person's kept feed answer only to a page render that proved the session
 * live with ITS OWN fresh /me and /me/permissions (lib/feed-cache.ts). These are the attacks run
 * against that rule; each one is a way a render could hold a proof it did not earn, or a kept
 * answer could reach someone it was not fetched for.
 *
 *   - THE COLLAPSE RACE (found here, fixed): /me was still collapsed into another render's
 *     in-flight /me. A render that started AFTER the session was revoked (or a role was taken
 *     away) could join a /me the API had answered BEFORE it, so it "proved" a dead session — or
 *     the old epoch — and was then served answers kept for up to 5 s. An identity read is now
 *     never collapsed for a person: every render's proof is its own request to the API.
 *   - two renders at once, one proven and one not (a route handler, a server action);
 *   - two people at once on one console server, same URL, same epoch;
 *   - work a render started BEFORE it proved the session (the org list);
 *   - a network-trusted console (every visitor sessionless): nothing is ever kept;
 *   - a POST whose path ends in /load but carries a body is a write, not a read twin.
 */
import { AsyncLocalStorage } from "node:async_hooks";
import { afterEach, describe, expect, it, vi } from "vitest";

const API = "http://api.test";

type G = { __bossFeedCache?: true; __bossIdentityFetch?: true; __bossOriginalFetch?: typeof fetch;
  __bossIdentityResolver?: unknown; __bossLiveProof?: unknown; fetch: typeof fetch;
  AsyncLocalStorage?: unknown };

(globalThis as unknown as G).AsyncLocalStorage ??= AsyncLocalStorage;

const served = new AsyncLocalStorage<{ cookie: string }>();
const resolver = async () => ({ cookie: served.getStore()?.cookie ?? "", csrf: null, origin: null,
                                clientHop: null });

async function install(original: typeof fetch) {
  vi.resetModules();
  vi.stubEnv("BOSS_FEED_CACHE_MS", "5000");
  const g = globalThis as unknown as G;
  delete g.__bossFeedCache; delete g.__bossIdentityFetch; delete g.__bossOriginalFetch;
  g.fetch = original;
  const cache = await import("../lib/feed-cache");
  const server = await import("../lib/identity/server-fetch");
  const forward = await import("../lib/identity/forward");
  cache.installFeedCache(API);
  server.installIdentityFetch(API);
  forward.installIdentityResolver(resolver as never);
  return { cached: g.fetch, cache };
}

afterEach(() => {
  vi.unstubAllEnvs();
  (globalThis as unknown as G).__bossIdentityResolver = undefined;
});

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** A page render as app/page.tsx does it: its own /me + /me/permissions, then the proof, then feeds. */
async function pageRender(cached: typeof fetch, cache: typeof import("../lib/feed-cache"),
                          token: string, feed: string) {
  const cookie = `__Host-boss_session=${token}`;
  return served.run({ cookie }, async () => {
    const [me, perms] = await Promise.all([cached(`${API}/v1/identity/me`),
                                           cached(`${API}/v1/identity/me/permissions`)]);
    if (!me.ok || !perms.ok) return { proved: false, feed: null as Response | null };
    const { epoch } = (await perms.json()) as { epoch: number };
    cache.enterLiveProof(await cache.liveProofFor(cookie, epoch));
    return { proved: true, feed: await cached(`${API}${feed}`) };
  });
}

describe("the live proof cannot be borrowed", () => {
  it("a render that starts after a revocation never joins an earlier render's in-flight /me", async () => {
    // The API: /me is slow (it is in flight when the revocation lands); it answers from the
    // state AT THE MOMENT IT WAS ASKED.
    let revoked = false;
    let feedCalls = 0;
    const { cached, cache } = await install((async (u: RequestInfo | URL) => {
      const url = String(u instanceof Request ? u.url : u);
      const deadAtAsk = revoked;
      if (url.includes("/v1/identity/")) await sleep(40);
      if (deadAtAsk) return new Response("{}", { status: 401 });
      if (url.endsWith("/v1/identity/me/permissions")) return new Response(JSON.stringify({ epoch: 7 }), { status: 200 });
      if (url.endsWith("/v1/identity/me")) return new Response(JSON.stringify({ id: "usr_carol" }), { status: 200 });
      feedCalls += 1;
      return new Response(JSON.stringify({ secret: "carol's packs" }), { status: 200 });
    }) as typeof fetch);
    // An earlier render keeps the feed for 5 s.
    expect((await pageRender(cached, cache, "bss_carol", "/v1/packs")).proved).toBe(true);
    expect(feedCalls).toBe(1);
    // Render A starts (its /me is in flight)…
    const a = pageRender(cached, cache, "bss_carol", "/v1/packs");
    await sleep(5);
    // …the session is revoked (SCIM, Sign out everywhere), and render B starts AFTER that.
    revoked = true;
    const b = await pageRender(cached, cache, "bss_carol", "/v1/packs");
    await a;
    expect(b.proved).toBe(false);
    expect(b.feed).toBeNull();
  });

  it("a proven render's kept answer never reaches a concurrent unproven render of the same session", async () => {
    let feedCalls = 0;
    const { cached, cache } = await install((async (u: RequestInfo | URL) => {
      const url = String(u instanceof Request ? u.url : u);
      if (url.endsWith("/me/permissions")) return new Response(JSON.stringify({ epoch: 3 }), { status: 200 });
      if (url.endsWith("/me")) return new Response("{}", { status: 200 });
      feedCalls += 1;
      await sleep(5);
      return new Response(JSON.stringify({ n: feedCalls }), { status: 200 });
    }) as typeof fetch);
    await pageRender(cached, cache, "bss_dana", "/v1/packs");
    // A route handler / server action of the same person, running alongside a proven render.
    const [, unproven] = await Promise.all([
      pageRender(cached, cache, "bss_dana", "/v1/packs"),
      served.run({ cookie: "__Host-boss_session=bss_dana" }, () => cached(`${API}/v1/packs`)),
    ]);
    expect(((await unproven.json()) as { n: number }).n).toBeGreaterThan(1);
  });

  it("two people on one console server never share an answer, in flight or kept, at the same epoch", async () => {
    const { cached, cache } = await install((async (u: RequestInfo | URL, init?: RequestInit) => {
      const url = String(u instanceof Request ? u.url : u);
      const cookie = new Headers(init?.headers ?? (u instanceof Request ? u.headers : undefined)).get("cookie") ?? "";
      if (url.endsWith("/me/permissions")) return new Response(JSON.stringify({ epoch: 9 }), { status: 200 });
      if (url.endsWith("/me")) return new Response("{}", { status: 200 });
      await sleep(10);
      return new Response(JSON.stringify({ for: cookie }), { status: 200 });
    }) as typeof fetch);
    const [x, y] = await Promise.all([pageRender(cached, cache, "bss_owner", "/v1/packs"),
                                      pageRender(cached, cache, "bss_viewer", "/v1/packs")]);
    expect(((await x.feed!.json()) as { for: string }).for).toContain("bss_owner");
    expect(((await y.feed!.json()) as { for: string }).for).toContain("bss_viewer");
    const again = await pageRender(cached, cache, "bss_viewer", "/v1/packs");
    expect(((await again.feed!.json()) as { for: string }).for).toContain("bss_viewer");
  });

  it("a fetch a render started BEFORE it proved the session reuses nothing kept", async () => {
    let orgCalls = 0;
    const { cached, cache } = await install((async (u: RequestInfo | URL) => {
      const url = String(u instanceof Request ? u.url : u);
      if (url.endsWith("/me/permissions")) return new Response(JSON.stringify({ epoch: 1 }), { status: 200 });
      if (url.endsWith("/me")) return new Response("{}", { status: 200 });
      orgCalls += 1;
      return new Response("{}", { status: 200 });
    }) as typeof fetch);
    await pageRender(cached, cache, "bss_dana", "/v1/orgs");          // kept under a proof
    await served.run({ cookie: "__Host-boss_session=bss_dana" }, async () => {
      const early = cached(`${API}/v1/orgs`);                        // the page's orgsP
      cache.enterLiveProof(await cache.liveProofFor("__Host-boss_session=bss_dana", 1));
      await early;
    });
    expect(orgCalls).toBe(2);
  });

  it("a network-trusted console keeps nothing for a sessionless visitor, whatever was 'proved'", async () => {
    vi.stubEnv("BOSS_CONSOLE_AUTH", "network");
    let calls = 0;
    const { cached, cache } = await install((async () => { calls += 1; return new Response("{}", { status: 200 }); }) as typeof fetch);
    vi.stubEnv("BOSS_CONSOLE_AUTH", "network");
    await served.run({ cookie: "" }, async () => {
      cache.enterLiveProof({ tag: "anon", epoch: 1 });
      await cached(`${API}/v1/packs`);
      await cached(`${API}/v1/packs`);
    });
    expect(calls).toBe(2);
  });

  it("a POST to a path ending in /load that carries a body is a write: never collapsed or kept", async () => {
    let calls = 0;
    const { cached, cache } = await install((async () => { calls += 1; return new Response("{}", { status: 200 }); }) as typeof fetch);
    await served.run({ cookie: "__Host-boss_session=bss_dana" }, async () => {
      cache.enterLiveProof(await cache.liveProofFor("__Host-boss_session=bss_dana", 1));
      const req = () => new Request(`${API}/v1/things/load`, { method: "POST", body: "{\"x\":1}" });
      await cached(req());
      await cached(req());
    });
    expect(calls).toBe(2);
  });
});
