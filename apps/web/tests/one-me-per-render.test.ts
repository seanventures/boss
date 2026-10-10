/**
 * ONE /v1/identity/me PER RENDER (perf: `overview duplicate_calls 1 > 0`).
 *
 * A render proves its session with its own fresh /me (the-live-proof-cannot-be-borrowed.test.ts).
 * A server-side write in the same render — a POST read twin — needs the session's CSRF token, and
 * instrumentation.ts used to fetch it with a SECOND /me whenever its kept copy was over 60 s old.
 * The render's own /me now carries the token to the write path (lib/identity/forward.ts
 * sessionCsrf), so each render asks /me exactly once — and still asks it itself, every time.
 */
import { AsyncLocalStorage } from "node:async_hooks";
import { afterEach, describe, expect, it, vi } from "vitest";

const API = "http://api.test";
const served = new AsyncLocalStorage<{ cookie: string }>();

vi.mock("next/headers", () => ({
  headers: async () => new Headers({ cookie: served.getStore()?.cookie ?? "" }),
}));

type G = Record<string, unknown> & { fetch: typeof fetch };

async function install(original: typeof fetch) {
  vi.resetModules();
  vi.stubEnv("BOSS_FEED_CACHE_MS", "5000");
  vi.stubEnv("BOSS_API_URL", API);
  vi.stubEnv("NEXT_RUNTIME", "nodejs");
  const g = globalThis as unknown as G;
  for (const k of ["__bossFeedCache", "__bossIdentityFetch", "__bossOriginalFetch", "__bossCsrfBySession",
                   "__bossIdentityResolver"]) delete g[k];
  g.fetch = original;
  const cache = await import("../lib/feed-cache");
  const server = await import("../lib/identity/server-fetch");
  cache.installFeedCache(API);
  server.installIdentityFetch(API);
  await (await import("../instrumentation")).register();   // the real resolver, CSRF path and all
  return { cached: g.fetch, cache };
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.useRealTimers();
  delete (globalThis as unknown as G).__bossIdentityResolver;
});

describe("one /me per render", () => {
  it("a render's write uses the CSRF token from its own /me, even long after the last one", async () => {
    const asked: string[] = [];
    const csrfSent: (string | null)[] = [];
    const { cached, cache } = await install((async (u: RequestInfo | URL, init?: RequestInit) => {
      const url = String(u instanceof Request ? u.url : u);
      const path = new URL(url).pathname;
      asked.push(`${init?.method ?? "GET"} ${path}`);
      if (path === "/v1/identity/me") return Response.json({ id: "p1", csrf_token: "csrf-of-s1" });
      if (path === "/v1/identity/me/permissions") return Response.json({ epoch: 3 });
      csrfSent.push(new Headers(init?.headers).get("x-boss-csrf"));
      return Response.json({ items: [] });
    }) as typeof fetch);

    const cookie = "__Host-boss_session=s1";
    const render = () => served.run({ cookie }, async () => {
      const [me, perms] = await Promise.all([cached(`${API}/v1/identity/me`),
                                             cached(`${API}/v1/identity/me/permissions`)]);
      const { epoch } = (await perms.json()) as { epoch: number };
      expect(me.ok).toBe(true);
      cache.enterLiveProof(await cache.liveProofFor(cookie, epoch));
      await cached(`${API}/v1/projects/p/overview/load`, { method: "POST" });   // a read twin: a write
    });

    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-09T10:00:00Z"));
    await render();
    vi.setSystemTime(new Date("2026-10-09T10:01:30Z"));   // past the 60 s a kept token lasts
    await render();

    const mes = asked.filter((a) => a === "GET /v1/identity/me");
    expect(mes).toHaveLength(2);                          // one per render: each its own, none extra
    expect(csrfSent).toEqual(["csrf-of-s1", "csrf-of-s1"]);
  });

  it("a write with no /me seen for its session still gets the token, with one /me of its own", async () => {
    let mes = 0;
    const { cached } = await install((async (u: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(u instanceof Request ? u.url : u)).pathname;
      if (path === "/v1/identity/me") { mes += 1; return Response.json({ csrf_token: "csrf-of-s2" }); }
      return Response.json({ sent: new Headers(init?.headers).get("x-boss-csrf") });
    }) as typeof fetch);
    const out = await served.run({ cookie: "__Host-boss_session=s2" },
      () => cached(`${API}/v1/things`, { method: "POST", body: "{}" }));
    expect(await out.json()).toEqual({ sent: "csrf-of-s2" });
    expect(mes).toBe(1);
  });
});
