/**
 * A previewed app is someone else's code served on the console's origin. The /v1 proxy must
 * never hand it the operator's session: no identity cookie, no CSRF token and no network-trust
 * marker reach a preview path, nothing the preview answers sets a cookie on the console's
 * origin, and the API's sandbox CSP reaches the browser intact. A request from a sandboxed
 * (`Origin: null`) or cross-site non-navigation context gets no session on ANY path, so a
 * preview's `fetch('/v1/identity/me')` cannot read the CSRF token.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { installIdentityResolver, isPreviewCapabilityPath, untrustedBrowserContext } from "../lib/identity/forward";

const SANDBOX = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads";
const COOKIES = "__Host-boss_session=sess_secret; __Host-boss_device=dev; other=1";

type Seen = { url: string; headers: Headers };
let seen: Seen[] = [];

function upstream(extra: Record<string, string> = {}, cookies: string[] = []) {
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    seen.push({ url: String(url), headers: new Headers(init?.headers) });
    const h = new Headers({ "content-type": "text/html", ...extra });
    for (const c of cookies) h.append("set-cookie", c);
    return new Response("<html></html>", { status: 200, headers: h });
  }));
}

/**
 * The route as production runs it: lib/client.ts wraps the global fetch (server-fetch.ts) so
 * every server-side call re-attaches the person from the incoming request — here, a resolver
 * that answers with the operator's session, as instrumentation.ts does. The proxy's decision to
 * speak for nobody must survive that wrapper.
 */
async function route() {
  vi.resetModules();
  delete (globalThis as { __bossIdentityFetch?: true }).__bossIdentityFetch;
  installIdentityResolver(async (write) => ({
    cookie: "__Host-boss_session=sess_secret", csrf: write ? "csrf_secret" : null, origin: "http://boss.example.test",
  }));
  return import("../app/v1/[...path]/route");
}

function ctx(path: string) {
  return { params: Promise.resolve({ path: path.replace(/^\/v1\//, "").split("/") }) };
}

describe("the /v1 proxy and previews", () => {
  beforeAll(() => {
    vi.stubEnv("BOSS_API_URL", "http://api.example.test");
    vi.stubEnv("BOSS_CONSOLE_AUTH", "network");
  });
  afterAll(() => { vi.unstubAllEnvs(); installIdentityResolver(async () => null); });
  beforeEach(() => { seen = []; vi.unstubAllGlobals(); });

  it("recognises preview capability paths, and only those", () => {
    for (const p of ["/v1/previews/prv_1/web", "/v1/previews/prv_1/web/", "/v1/previews/prv_1/web/a/b.js",
                     "/v1/previews/prv_1/feedback"]) {
      expect(isPreviewCapabilityPath(p), p).toBe(true);
    }
    for (const p of ["/v1/previews/prv_1/stop", "/v1/previews/prv_1/logs", "/v1/previews", "/v1/identity/me",
                     "/v1/previews/prv_1/webby", "/v1/previews/prv_1/gate"]) {
      expect(isPreviewCapabilityPath(p), p).toBe(false);
    }
  });

  for (const method of ["GET", "POST"] as const) {
    it(`${method} to a preview path forwards no cookie, CSRF or network marker`, async () => {
      upstream({ "content-security-policy": SANDBOX, "access-control-allow-origin": "*" });
      const { GET, POST } = await route();
      const path = "/v1/previews/prv_1/web/api/items";
      const req = new Request(`http://boss.example.test${path}`, {
        method,
        headers: { cookie: COOKIES, "x-boss-csrf": "csrf_secret", origin: "http://boss.example.test", "content-type": "application/json" },
        body: method === "POST" ? "{}" : undefined,
      });
      const res = await (method === "GET" ? GET : POST)(req, ctx(path));
      expect(seen).toHaveLength(1);
      expect(seen[0].url).toBe(`http://api.example.test${path}`);
      expect(seen[0].headers.get("cookie")).toBeNull();
      expect(seen[0].headers.get("x-boss-csrf")).toBeNull();
      expect(seen[0].headers.get("x-boss-console-auth")).toBeNull();
      // The isolation reaches the browser.
      expect(res.headers.get("content-security-policy")).toBe(SANDBOX);
      expect(res.headers.get("content-security-policy")).not.toContain("allow-same-origin");
      expect(res.headers.get("access-control-allow-origin")).toBe("*");
    });
  }

  it("never relays a cookie the previewed app tries to set on the console's origin", async () => {
    upstream({ "content-security-policy": SANDBOX }, ["__Host-boss_session=attacker; Path=/; Secure; HttpOnly"]);
    const { GET } = await route();
    const path = "/v1/previews/prv_1/web/";
    const res = await GET(new Request(`http://boss.example.test${path}`, { headers: { cookie: COOKIES } }), ctx(path));
    expect(res.headers.get("set-cookie")).toBeNull();
  });

  it("a sandboxed document's fetch of /v1/identity/me goes without the session", async () => {
    upstream();
    const { GET } = await route();
    const path = "/v1/identity/me";
    await GET(new Request(`http://boss.example.test${path}`, {
      headers: { cookie: COOKIES, origin: "null", "sec-fetch-site": "cross-site", "sec-fetch-mode": "cors" },
    }), ctx(path));
    expect(seen[0].headers.get("cookie")).toBeNull();
    expect(seen[0].headers.get("x-boss-console-auth")).toBeNull();
  });

  it("the console's own same-origin call still carries the person", async () => {
    upstream();
    const { GET } = await route();
    const path = "/v1/identity/me";
    await GET(new Request(`http://boss.example.test${path}`, {
      headers: { cookie: COOKIES, "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors" },
    }), ctx(path));
    expect(seen[0].headers.get("cookie")).toContain("__Host-boss_session=sess_secret");
    expect(seen[0].headers.get("x-boss-console-auth")).toBe("network");
  });

  it("an IdP's cross-site redirect back (a navigation) keeps its cookies", () => {
    expect(untrustedBrowserContext(new Headers({ "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate" }))).toBe(false);
    expect(untrustedBrowserContext(new Headers({ "sec-fetch-site": "cross-site", "sec-fetch-mode": "cors" }))).toBe(true);
    expect(untrustedBrowserContext(new Headers({ origin: "null" }))).toBe(true);
    expect(untrustedBrowserContext(new Headers({ "sec-fetch-site": "same-origin" }))).toBe(false);
  });

  it("answers a preview's preflight without credentials, and nothing else's", async () => {
    const { OPTIONS } = await route();
    const ok = await OPTIONS(new Request("http://boss.example.test/v1/previews/prv_1/feedback", {
      method: "OPTIONS", headers: { "access-control-request-headers": "content-type, x-boss-csrf, authorization" },
    }));
    expect(ok.status).toBe(204);
    expect(ok.headers.get("access-control-allow-origin")).toBe("*");
    expect(ok.headers.get("access-control-allow-credentials")).toBeNull();
    expect(ok.headers.get("access-control-allow-headers")).toBe("content-type");
    const no = await OPTIONS(new Request("http://boss.example.test/v1/identity/me", { method: "OPTIONS" }));
    expect(no.status).toBe(405);
  });
});
