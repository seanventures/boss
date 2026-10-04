/**
 * The feed cache hands every caller its own readable body, and never reuses Repo and models.
 *
 * ~1 load in 8 showed a red "Response.clone: Body has already been consumed" and a 502: the
 * server render and /api/setup asked for the same URL together, and the joiners cloned one
 * shared Response while another reader drained it. And a re-read straight after Replace token
 * got the 5-second-old view.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

const API = "http://api.test";

async function install(original: typeof fetch) {
  vi.resetModules();
  const g = globalThis as unknown as { __bossFeedCache?: true; fetch: typeof fetch };
  delete g.__bossFeedCache;
  g.fetch = original;
  const { installFeedCache } = await import("../lib/feed-cache");
  installFeedCache(API);
  return g.fetch;
}

afterEach(() => { vi.unstubAllGlobals(); });

describe("the feed cache", () => {
  it("gives concurrent callers of one URL each a body they can read, even as others drain", async () => {
    let calls = 0;
    const cached = await install((async () => {
      calls += 1;
      await new Promise((r) => setTimeout(r, 20));
      return new Response(JSON.stringify({ n: calls }), { status: 200 });
    }) as typeof fetch);
    const url = `${API}/v1/feed?x=1`;
    const answers = await Promise.all(Array.from({ length: 12 }, async () => {
      const r = await cached(url);
      return r.json();
    }));
    expect(calls).toBe(1);
    expect(answers.every((a) => a.n === 1)).toBe(true);
    // A completed entry is reused, and still readable many times over.
    for (let i = 0; i < 5; i += 1) expect((await (await cached(url)).json()).n).toBe(1);
  });

  it("never reuses a Repo and models read", async () => {
    let calls = 0;
    const cached = await install((async () => {
      calls += 1;
      return new Response(JSON.stringify({ n: calls }), { status: 200 });
    }) as typeof fetch);
    const url = `${API}/v1/setup?project_id=p`;
    expect((await (await cached(url)).json()).n).toBe(1);
    expect((await (await cached(url)).json()).n).toBe(2);
  });
});
