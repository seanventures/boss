/**
 * A REVOKED SESSION IS NEVER ANSWERED FROM THE FEED CACHE (round 3, R3-STACK S2).
 *
 * On the real stack SCIM deactivated a person and the API refused her session at once, but her
 * browser's GET /v1/identity/me through the console answered 200 — with her name — for five more
 * seconds: lib/feed-cache.ts kept every successful read per (URL, session) and served it again
 * without asking the API. Measured: the same cookie, the API direct 401, through the console 200,
 * and a cache-busting query string made the console say 401 too.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

const API = "http://api.test";

async function install(original: typeof fetch) {
  vi.resetModules();
  vi.stubEnv("BOSS_FEED_CACHE_MS", "5000");
  const g = globalThis as unknown as { __bossFeedCache?: true; fetch: typeof fetch };
  delete g.__bossFeedCache;
  g.fetch = original;
  const mod = await import("../lib/feed-cache");
  mod.installFeedCache(API);
  return { cached: g.fetch, NO_REUSE_HEADER: mod.NO_REUSE_HEADER };
}

afterEach(() => { vi.unstubAllEnvs(); });

const as = (token: string, extra: Record<string, string> = {}) => ({ headers: { cookie: `__Host-boss_session=${token}`, ...extra } });

describe("the feed cache and a person's session", () => {
  it("asks the API again after a person's read returned: the revocation is seen on the next call", async () => {
    let revoked = false;
    const { cached } = await install((async () => new Response(JSON.stringify({ ok: !revoked }),
      { status: revoked ? 401 : 200 })) as typeof fetch);
    const me = `${API}/v1/identity/me`;
    expect((await cached(me, as("bss_carol"))).status).toBe(200);
    revoked = true;                                   // SCIM active=false, sessions revoked
    expect((await cached(me, as("bss_carol"))).status).toBe(401);
  });

  it("still collapses one person's identical reads while they are in flight", async () => {
    let calls = 0;
    const { cached } = await install((async () => {
      calls += 1;
      await new Promise((r) => setTimeout(r, 20));
      return new Response("{}", { status: 200 });
    }) as typeof fetch);
    await Promise.all(Array.from({ length: 6 }, () => cached(`${API}/v1/orgs`, as("bss_a"))));
    expect(calls).toBe(1);
  });

  it("keeps a sessionless read for the TTL (nothing to revoke)", async () => {
    let calls = 0;
    const { cached } = await install((async () => { calls += 1; return new Response("{}", { status: 200 }); }) as typeof fetch);
    await cached(`${API}/v1/edition`);
    await cached(`${API}/v1/edition`);
    expect(calls).toBe(1);
  });

  it("never collapses or keeps the browser's own call through the /v1 proxy, and never sends the marker", async () => {
    const seen: (string | null)[] = [];
    let calls = 0;
    const { cached, NO_REUSE_HEADER } = await install((async (_u: RequestInfo | URL, init?: RequestInit) => {
      calls += 1;
      seen.push(new Headers(init?.headers).get(NO_REUSE_HEADER));
      await new Promise((r) => setTimeout(r, 10));
      return new Response("{}", { status: 200 });
    }) as typeof fetch);
    const anon = { headers: { [NO_REUSE_HEADER]: "1" } };
    await Promise.all([cached(`${API}/v1/edition`, anon), cached(`${API}/v1/edition`, anon)]);
    await cached(`${API}/v1/edition`, anon);
    expect(calls).toBe(3);
    expect(seen.every((v) => v === null)).toBe(true);
  });

  it("the /v1 proxy marks every upstream call", async () => {
    const { readFileSync } = await import("node:fs");
    const src = readFileSync(new URL("../app/v1/[...path]/route.ts", import.meta.url), "utf8");
    expect(src).toMatch(/outbound\.set\(NO_REUSE_HEADER, "1"\)/);
    expect(src).toMatch(/headers: outbound,/);
  });
});
