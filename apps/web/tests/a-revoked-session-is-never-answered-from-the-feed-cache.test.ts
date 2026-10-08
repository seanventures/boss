/**
 * A REVOKED SESSION IS NEVER ANSWERED FROM THE FEED CACHE (round 3, R3-STACK S2).
 *
 * On the real stack SCIM deactivated a person and the API refused her session at once, but her
 * browser's GET /v1/identity/me through the console answered 200 — with her name — for five more
 * seconds: lib/feed-cache.ts kept every successful read per (URL, session) and served it again
 * without asking the API. Measured: the same cookie, the API direct 401, through the console 200,
 * and a cache-busting query string made the console say 401 too.
 *
 * id-sec-final2 B1/B2: this file now installs BOTH wrappers in lib/client.ts's order (the feed
 * cache inner, the identity fetch outer) — the earlier version installed only the cache, so it
 * could not see that the identity wrapper stripped the /v1 proxy's no-reuse HEADER before the
 * cache read it. The mark now rides on the RequestInit. A sessionless answer is keyed by the
 * client's hop and pre-auth cookie, never kept on a network-trusted console, and never kept when
 * the API says no-store.
 */
import { AsyncLocalStorage } from "node:async_hooks";
import { afterEach, describe, expect, it, vi } from "vitest";

const API = "http://api.test";

type G = { __bossFeedCache?: true; __bossIdentityFetch?: true; __bossOriginalFetch?: typeof fetch;
  __bossIdentityResolver?: unknown; fetch: typeof fetch };

/** Exactly as lib/client.ts installs them: the feed cache first (inner), the identity fetch outer. */
async function install(original: typeof fetch, resolver?: (write: boolean) => Promise<unknown>) {
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
  if (resolver) forward.installIdentityResolver(resolver as never);
  else g.__bossIdentityResolver = undefined;
  return { cached: g.fetch, noReuse: cache.noReuse };
}

afterEach(() => {
  vi.unstubAllEnvs();
  (globalThis as unknown as G).__bossIdentityResolver = undefined;
});

/** The request being served, as instrumentation.ts's resolver reads it (next/headers). */
const served = new AsyncLocalStorage<{ cookie: string; hop?: string | null }>();
const resolver = async () => ({ cookie: served.getStore()?.cookie ?? "", csrf: null, origin: null,
                                clientHop: served.getStore()?.hop ?? null });
/** Run `fn` while serving a request of the person holding `token`. */
const asPerson = <T>(token: string, fn: () => Promise<T>) => served.run({ cookie: `__Host-boss_session=${token}` }, fn);

describe("the feed cache and a person's session", () => {
  it("asks the API again after a person's read returned: the revocation is seen on the next call", async () => {
    let revoked = false;
    const { cached } = await install((async () => new Response(JSON.stringify({ ok: !revoked }),
      { status: revoked ? 401 : 200 })) as typeof fetch, resolver);
    const me = `${API}/v1/identity/me`;
    expect((await asPerson("bss_carol", () => cached(me))).status).toBe(200);
    revoked = true;                                   // SCIM active=false, sessions revoked
    expect((await asPerson("bss_carol", () => cached(me))).status).toBe(401);
  });

  it("still collapses one person's identical reads while they are in flight", async () => {
    let calls = 0;
    const { cached } = await install((async () => {
      calls += 1;
      await new Promise((r) => setTimeout(r, 20));
      return new Response("{}", { status: 200 });
    }) as typeof fetch, resolver);
    await Promise.all(Array.from({ length: 6 }, () => asPerson("bss_a", () => cached(`${API}/v1/orgs`))));
    expect(calls).toBe(1);
  });

  it("keeps a sessionless read for the TTL (nothing to revoke)", async () => {
    let calls = 0;
    const { cached } = await install((async () => { calls += 1; return new Response("{}", { status: 200 }); }) as typeof fetch);
    await cached(`${API}/v1/edition`);
    await cached(`${API}/v1/edition`);
    expect(calls).toBe(1);
  });

  it("B1: never collapses or keeps the browser's own call, through the REAL wrapper order", async () => {
    let calls = 0;
    const { cached, noReuse } = await install((async () => {
      calls += 1;
      await new Promise((r) => setTimeout(r, 10));
      return new Response(JSON.stringify({ n: calls }), { status: 200 });
    }) as typeof fetch, resolver);
    await Promise.all([cached(`${API}/v1/auth/options`, noReuse({})), cached(`${API}/v1/auth/options`, noReuse({}))]);
    await cached(`${API}/v1/auth/options`, noReuse({}));
    expect(calls).toBe(3);
    // …and for a person too: no in-flight join
    calls = 0;
    await asPerson("bss_x", () => Promise.all([cached(`${API}/v1/identity/me`, noReuse({})),
                                               cached(`${API}/v1/identity/me`, noReuse({}))]));
    expect(calls).toBe(2);
  });

  it("B1: the /v1 proxy marks every upstream call on its init", async () => {
    const { readFileSync } = await import("node:fs");
    const src = readFileSync(new URL("../app/v1/[...path]/route.ts", import.meta.url), "utf8");
    expect(src).toMatch(/upstream = await fetch\(target, noReuse\(\{/);
    expect(src).toMatch(/headers: outbound,/);
  });

  it("B2: two sessionless clients never share an answer (client hop, pre-auth cookie)", async () => {
    const als = served;
    let calls = 0;
    const { cached } = await install((async (_u: RequestInfo | URL, init?: RequestInit) => {
      calls += 1;
      const h = new Headers(init?.headers);
      return new Response(JSON.stringify({ for: h.get("x-forwarded-for") ?? "-", c: h.get("cookie") ?? "-" }), { status: 200 });
    }) as typeof fetch, resolver);
    const a = await (await als.run({ hop: "10.0.0.1", cookie: "" }, () => cached(`${API}/v1/edition`))).json();
    const b = await (await als.run({ hop: "203.0.113.9", cookie: "" }, () => cached(`${API}/v1/edition`))).json();
    expect(a.for).toBe("10.0.0.1");
    expect(b.for).toBe("203.0.113.9");
    const c = await (await als.run({ hop: "203.0.113.9", cookie: "__Host-boss_preauth=AAA" }, () => cached(`${API}/v1/edition`))).json();
    const d = await (await als.run({ hop: "203.0.113.9", cookie: "__Host-boss_preauth=BBB" }, () => cached(`${API}/v1/edition`))).json();
    expect(c.c).toContain("AAA");
    expect(d.c).toContain("BBB");
    expect(calls).toBe(4);
  });

  it("B2: a server-side render forwards the client hop, never a client's own chain", async () => {
    let seen: string | null = "unset";
    const { cached } = await install((async (_u: RequestInfo | URL, init?: RequestInit) => {
      seen = new Headers(init?.headers).get("x-forwarded-for");
      return new Response("{}", { status: 200 });
    }) as typeof fetch, async () => ({ cookie: "", csrf: null, origin: null, clientHop: "198.51.100.7" }));
    await cached(`${API}/v1/edition`, { headers: { "x-forwarded-for": "10.0.0.1, 198.51.100.7" } });
    expect(seen).toBe("198.51.100.7");
  });

  it("B2: never keeps a sessionless answer on a network-trusted console, or one marked no-store", async () => {
    let calls = 0;
    const { cached } = await install((async (u: RequestInfo | URL) => {
      calls += 1;
      const noStore = String(u).endsWith("/v1/auth/options");
      return new Response("{}", { status: 200, headers: noStore ? { "cache-control": "no-store" } : {} });
    }) as typeof fetch);
    await cached(`${API}/v1/auth/options`);
    await cached(`${API}/v1/auth/options`);
    expect(calls).toBe(2);
    calls = 0;
    vi.stubEnv("BOSS_CONSOLE_AUTH", "network");
    await cached(`${API}/v1/edition`);
    await cached(`${API}/v1/edition`);
    expect(calls).toBe(2);
  });
});
