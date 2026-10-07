/**
 * THE CONSOLE HOLDS NO SHARED CREDENTIAL (IDENTITY.md §4.1, D1, §12 M3, A9).
 *
 * The console used to attach one `BOSS_API_KEY` to every call and run its own HMAC session from
 * `BOSS_CONSOLE_PASSWORD`. Now every call carries the signed-in person's own cookie: nothing in
 * apps/web reads those variables, the /v1 proxy forwards only the identity cookies (plus the CSRF
 * token), strips everything that could name a principal, relays the API's Set-Cookie, and the
 * feed cache never hands one person's answer to another.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { identityCookieHeader, isStrippedHeader, sessionTag, withForwardedIdentity, installIdentityResolver } from "../lib/identity/forward";

const ROOT = process.cwd();
function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(join(ROOT, dir))) {
    if (name === "node_modules" || name.startsWith(".") || name.startsWith("__")) continue;
    const path = join(dir, name);
    if (statSync(join(ROOT, path)).isDirectory()) out.push(...walk(path));
    else if (/\.(ts|tsx)$/.test(name)) out.push(path);
  }
  return out;
}
const SOURCE = [...walk("app"), ...walk("components"), ...walk("lib"), "middleware.ts", "instrumentation.ts", "next.config.ts"];
/** Source with comments removed: a comment may name the old variable to say it is gone. */
const code = (f: string) => readFileSync(join(ROOT, f), "utf8").replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`\\])\/\/[^\n]*/g, "$1");

afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); vi.resetModules(); installIdentityResolver(async () => null); });

describe("no shared credential is read anywhere in the console", () => {
  it.each(["BOSS_API_KEY", "NEXT_PUBLIC_BOSS_API_KEY", "BOSS_CONSOLE_PASSWORD", "BOSS_CONSOLE_SESSION_SECRET"])("%s", (name) => {
    const readers = SOURCE.filter((f) => new RegExp(`process\\.env\\.${name}\\b|env\\[["']${name}["']\\]`).test(code(f)));
    expect(readers).toEqual([]);
  });

  it("no source builds an Authorization header for the API", () => {
    const bad = SOURCE.filter((f) => /Authorization:\s*`Bearer/.test(code(f)));
    expect(bad).toEqual([]);
  });

  it("the typed client is created without an apiKey", () => {
    expect(code("lib/client.ts")).not.toMatch(/apiKey:/);
  });
});

describe("what the pass-through forwards", () => {
  it("strips Authorization, every X-Boss-* but the CSRF token, and identity headers", () => {
    for (const h of ["authorization", "Authorization", "x-boss-api-key", "x-boss-principal", "x-boss-intent", "x-boss-path", "x-user-id",
      "x-forwarded-user", "remote-user", "tailscale-user-login", "x-auth-request-email", "cf-access-authenticated-user-email", "cookie"]) {
      expect(isStrippedHeader(h), h).toBe(true);
    }
    for (const h of ["x-boss-csrf", "X-Boss-CSRF", "content-type", "idempotency-key", "accept", "origin"]) {
      expect(isStrippedHeader(h), h).toBe(false);
    }
  });

  it("forwards only the identity cookies", () => {
    const header = "theme=dark; __Host-boss_session=bss_a; boss_console_session_local=old; __Host-boss_preauth=p; __Host-boss_device=d; boss_stub_claim=network_bridge";
    expect(identityCookieHeader(header)).toBe("__Host-boss_session=bss_a; __Host-boss_preauth=p; __Host-boss_device=d");
    expect(identityCookieHeader(header, { stub: true })).toContain("boss_stub_claim=network_bridge");
  });

  it("a server-side write carries the person's cookie, CSRF and origin — and drops a caller's bearer", async () => {
    installIdentityResolver(async (write) => ({ cookie: "__Host-boss_session=bss_a", csrf: write ? "csrf_1" : null, origin: "https://boss.example" }));
    const h = await withForwardedIdentity("POST", { Authorization: "Bearer boss_pat_x", "Content-Type": "application/json", "X-Boss-Principal": "usr_owner" });
    expect(h.get("authorization")).toBeNull();
    expect(h.get("x-boss-principal")).toBeNull();
    expect(h.get("cookie")).toBe("__Host-boss_session=bss_a");
    expect(h.get("x-boss-csrf")).toBe("csrf_1");
    expect(h.get("origin")).toBe("https://boss.example");
    expect(h.get("sec-fetch-site")).toBe("same-origin");
    const read = await withForwardedIdentity("GET", undefined);
    expect(read.get("x-boss-csrf")).toBeNull();
  });

  it("the browser proxy forwards the cookie, strips identity headers and relays Set-Cookie", async () => {
    vi.stubEnv("BOSS_API_URL", "http://api.internal:8000");
    const seen: Headers[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
      seen.push(new Headers(init.headers));
      const res = new Headers({ "content-type": "application/json" });
      res.append("set-cookie", "__Host-boss_session=bss_new; Path=/; HttpOnly; Secure; SameSite=Lax");
      res.append("set-cookie", "__Host-boss_preauth=; Path=/; Max-Age=0");
      return new Response("{}", { status: 200, headers: res });
    }));
    // In a request, instrumentation.ts's resolver reads the incoming cookies (next/headers); the
    // server-side fetch wrapper then re-applies them. Here the resolver returns what it would.
    installIdentityResolver(async () => ({ cookie: identityCookieHeader("__Host-boss_preauth=p; theme=dark"), csrf: null, origin: "https://boss.example" }));
    const { POST } = await import("../app/v1/[...path]/route");
    const req = new Request("https://boss.example/v1/auth/mfa/totp", {
      method: "POST",
      headers: {
        cookie: "__Host-boss_preauth=p; theme=dark", authorization: "Bearer boss_pat_stolen", "x-boss-principal": "usr_owner",
        "x-boss-csrf": "c1", "x-user-id": "usr_owner", origin: "https://boss.example", "content-type": "application/json",
      },
      body: JSON.stringify({ code: "123456" }),
    });
    const res = await POST(req, { params: Promise.resolve({ path: ["auth", "mfa", "totp"] }) });
    const h = seen[0];
    expect(h.get("cookie")).toBe("__Host-boss_preauth=p");
    expect(h.get("authorization")).toBeNull();
    expect(h.get("x-boss-principal")).toBeNull();
    expect(h.get("x-user-id")).toBeNull();
    expect(h.get("x-boss-csrf")).toBe("c1");
    expect(h.get("origin")).toBe("https://boss.example");
    expect(res.headers.getSetCookie()).toEqual([
      "__Host-boss_session=bss_new; Path=/; HttpOnly; Secure; SameSite=Lax", "__Host-boss_preauth=; Path=/; Max-Age=0",
    ]);
  });
});

describe("the feed cache partitions by session", () => {
  it("two people's identical reads are two requests; one person's repeat is asked again (never reused)", async () => {
    vi.stubEnv("BOSS_FEED_CACHE_MS", "5000");
    let calls = 0;
    const g = globalThis as unknown as { fetch: typeof fetch; __bossFeedCache?: true };
    const before = g.fetch;
    delete g.__bossFeedCache;
    g.fetch = (async () => { calls += 1; return new Response(JSON.stringify({ n: calls }), { status: 200 }); }) as typeof fetch;
    try {
      const { installFeedCache } = await import("../lib/feed-cache");
      installFeedCache("http://api.internal:8000");
      const as = (token: string) => ({ headers: { cookie: `__Host-boss_session=${token}` } });
      const a1 = await (await g.fetch("http://api.internal:8000/v1/orgs", as("bss_alice"))).json();
      const a2 = await (await g.fetch("http://api.internal:8000/v1/orgs", as("bss_alice"))).json();
      const b1 = await (await g.fetch("http://api.internal:8000/v1/orgs", as("bss_bob"))).json();
      // A person's answer is not kept after it returned (round 3, S2: a revoked session's reads
      // were answered from this cache for 5 s); two people never share one.
      expect(a2).not.toEqual(a1);
      expect(b1).not.toEqual(a1);
      expect(calls).toBe(3);
    } finally {
      g.fetch = before;
      delete g.__bossFeedCache;
    }
  });

  it("the tag is not the token", () => {
    const tag = sessionTag(new Headers({ cookie: "__Host-boss_session=bss_secret_value" }));
    expect(tag).not.toContain("secret");
    expect(sessionTag(new Headers())).toBe("anon");
  });
});
