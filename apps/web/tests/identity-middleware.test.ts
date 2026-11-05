/**
 * MIDDLEWARE ROUTES, IT DOES NOT DECIDE (IDENTITY.md §4.1). No session → the sign-in page for a
 * page and a 401 for an API call; the sign-in pages and the public sign-in API pass; a
 * network-trusted build passes (the API's bridge decides who the visitor is); the old identity
 * area redirects to /identity; the pathname header layouts guard on is set by the server only;
 * pages carry the nonce CSP (A10), API responses do not.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { contentSecurityPolicy, middleware } from "../middleware";
import { PATH_HEADER, SEARCH_HEADER } from "../lib/identity/pages";

afterEach(() => vi.unstubAllEnvs());

const ORIGIN = "https://boss.example";
function remote(path: string, init: { cookie?: string; method?: string; headers?: Record<string, string> } = {}) {
  vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", ORIGIN);
  const headers: Record<string, string> = { host: "boss.example", ...(init.headers ?? {}) };
  if (init.cookie) headers.cookie = init.cookie;
  return new NextRequest(`${ORIGIN}${path}`, { method: init.method ?? "GET", headers });
}
const SESSION = "__Host-boss_session=bss_x";
/** What middleware forwarded to the route as a request header (NextResponse.next override). */
const forwarded = (r: Response, name: string) => r.headers.get(`x-middleware-request-${name.toLowerCase()}`);

describe("no session", () => {
  it.each(["/", "/identity", "/identity/people/usr_1", "/account/keys", "/?area=infra"])("a page %s goes to sign in, keeping the place", async (path) => {
    const r = await middleware(remote(path));
    expect(r.status).toBe(307);
    const to = new URL(r.headers.get("location")!);
    expect(to.origin + to.pathname).toBe(`${ORIGIN}/login`);
    expect(to.searchParams.get("next")).toBe(path.split("?")[0] + (path.includes("?") ? `?${path.split("?")[1]}` : ""));
    expect(r.headers.get("cache-control")).toMatch(/no-store/);
  });

  it.each(["/v1/projects", "/v1/identity/me", "/api/projects-strip?project_id=x", "/api/skills"])("an API call %s is a 401, before any handler", async (path) => {
    const r = await middleware(remote(path));
    expect(r.status).toBe(401);
    expect(await r.json()).toEqual({ error: "unauthenticated" });
  });

  it.each(["/login", "/claim", "/invite", "/signin", "/signin/link", "/reset", "/signup", "/boss-mark.png",
    "/v1/auth/options", "/v1/auth/login", "/v1/auth/webauthn/options", "/v1/auth/sso/idp_okta/start"])("public %s passes", async (path) => {
    const r = await middleware(remote(path, { method: path.startsWith("/v1/auth/") && path !== "/v1/auth/options" && !path.includes("/sso/") ? "POST" : "GET",
      headers: { origin: ORIGIN } }));
    expect(r.headers.get("x-middleware-next")).toBe("1");
  });

  it("an identity API path that is not sign-in is NOT public", async () => {
    for (const p of ["/v1/auth/logout", "/v1/auth/step-up", "/v1/identity/users", "/enrol"]) {
      expect((await middleware(remote(p))).status, p).not.toBe(200);
    }
  });
});

describe("the GitHub App update callback", () => {
  const path = "/v1/identity/github/app/installed?installation_id=424242&setup_action=update";
  const crossSite = { "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document" };

  it("a cross-site navigation without a cookie is a same-origin retry, not a 401", async () => {
    const r = await middleware(remote(path, { headers: crossSite }));
    expect(r.status).toBe(200);
    expect(r.headers.get("content-type")).toMatch(/text\/html/);
    expect(r.headers.get("cache-control")).toMatch(/no-store/);
    const html = await r.text();
    expect(html).toContain("/v1/identity/github/app/installed?installation_id=424242&amp;setup_action=update");
    expect(html).not.toContain("<script");
    expect(html).not.toContain("424242%27");
  });

  it("does not echo a query that is not an installation id", async () => {
    const r = await middleware(remote(
      "/v1/identity/github/app/installed?installation_id=%3Cscript%3E&setup_action=update",
      { headers: crossSite }));
    expect(r.status).toBe(401);
    expect(await r.text()).not.toContain("script");
  });

  it("a direct call and a same-origin retry without a cookie stay unauthenticated", async () => {
    const direct = await middleware(remote(path));
    expect(direct.status).toBe(401);
    expect(await direct.json()).toEqual({ error: "unauthenticated" });
    const again = await middleware(remote(path, { headers: { "sec-fetch-site": "same-origin", "sec-fetch-mode": "navigate" } }));
    expect(again.status).toBe(401);
    expect(await again.json()).toEqual({ error: "unauthenticated" });
  });

  it("a cross-site form POST is the same retry", async () => {
    const r = await middleware(remote(path, { method: "POST", headers: crossSite }));
    expect(r.status).toBe(200);
    expect(await r.text()).toContain("installation_id=424242");
  });

  it("a session cookie is passed through for the API to confirm", async () => {
    const r = await middleware(remote(path, { cookie: SESSION, headers: crossSite }));
    expect(r.headers.get("x-middleware-next")).toBe("1");
  });
});

describe("with a session cookie", () => {
  it("passes pages and API calls through: the API decides who it is", async () => {
    for (const p of ["/", "/identity", "/v1/identity/me", "/api/skills"]) {
      const r = await middleware(remote(p, { cookie: SESSION }));
      expect(r.headers.get("x-middleware-next"), p).toBe("1");
    }
  });

  it("still refuses a cross-site write", async () => {
    const r = await middleware(remote("/v1/projects", { cookie: SESSION, method: "POST", headers: { origin: "https://evil.example" } }));
    expect(r.status).toBe(403);
  });
});

describe("a network-trusted build", () => {
  it("passes without a cookie (the API's network bridge decides)", async () => {
    vi.stubEnv("BOSS_CONSOLE_AUTH", "network");
    const r = await middleware(new NextRequest("http://boss-host.internal:3000/identity", { headers: { host: "boss-host.internal:3000" } }));
    expect(r.headers.get("x-middleware-next")).toBe("1");
  });
});

describe("the old identity area", () => {
  it("?area=enterprise answers 307 to /identity, keeping the org", async () => {
    const r = await middleware(remote("/?area=enterprise&org=org_1", { cookie: SESSION, headers: { "x-forwarded-host": "boss.example", "x-forwarded-proto": "https" } }));
    expect(r.status).toBe(307);
    const to = new URL(r.headers.get("location")!);
    expect(to.pathname).toBe("/identity");
    expect(to.searchParams.get("org")).toBe("org_1");
  });

  it("?area=sso lands on Sign-in & SSO", async () => {
    const r = await middleware(remote("/?area=sso", { cookie: SESSION }));
    expect(new URL(r.headers.get("location")!).pathname).toBe("/identity/sign-in");
  });
});

describe("the pathname header layouts guard on", () => {
  it("is set from the URL, and a client's own copy is overwritten", async () => {
    const r = await middleware(remote("/identity/roles", { cookie: SESSION, headers: { [PATH_HEADER]: "/identity" } }));
    expect(forwarded(r, PATH_HEADER)).toBe("/identity/roles");
  });

  it("forwards the query, and a client's own copy is overwritten", async () => {
    const r = await middleware(remote("/v2/timeline?org=org_boss", {
      cookie: SESSION, headers: { [SEARCH_HEADER]: "?area=nope" },
    }));
    expect(forwarded(r, SEARCH_HEADER)).toBe("?org=org_boss");
  });

  it("an empty query clears a client's search header", async () => {
    const r = await middleware(remote("/v2/timeline", {
      cookie: SESSION, headers: { [SEARCH_HEADER]: "?org=org_evil" },
    }));
    expect(forwarded(r, SEARCH_HEADER)).toBe("");
  });
});

describe("the content security policy (A10)", () => {
  it("pages carry a nonce CSP with strict-dynamic and no framing", async () => {
    const r = await middleware(remote("/identity", { cookie: SESSION }));
    const csp = r.headers.get("content-security-policy") ?? "";
    expect(csp).toMatch(/script-src 'self' 'nonce-[^']+' 'strict-dynamic'/);
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).not.toContain("unsafe-eval");
  });

  it("API responses carry none", async () => {
    for (const p of ["/v1/identity/me", "/api/skills"]) {
      const r = await middleware(remote(p, { cookie: SESSION }));
      expect(r.headers.get("content-security-policy"), p).toBeNull();
    }
  });

  it("the policy builder is strict outside dev", () => {
    expect(contentSecurityPolicy("abc", false)).toContain("'nonce-abc'");
    expect(contentSecurityPolicy("abc", false)).not.toContain("unsafe-eval");
    expect(contentSecurityPolicy("abc", true)).toContain("unsafe-eval");
  });
});
