/**
 * SAML AND SCIM ON THE PUBLIC ORIGIN (round 3, W2; J7). An IdP knows BOSS by one address, the
 * console's. `/saml/v2/metadata`, the ACS and `/scim/v2/*` are forwarded to the API — each with
 * only its own protocol's credential:
 *
 *   - SCIM: `Authorization` passes through; no cookie, no CSRF, no Origin, no X-Boss-*; a browser
 *     request (Origin / Sec-Fetch-*) is refused.
 *   - SAML ACS: the one write allowed cross-site, at exactly `/saml/v2/acs`; identity cookies only;
 *     the API's Set-Cookie and Location reach the browser.
 *   - Nothing else widens: other cross-site writes, other spellings, other methods are refused as before.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { existsSync, readFileSync } from "node:fs";
import { NextRequest } from "next/server";

import { middleware } from "../middleware";
import {
  SAML_ACS_PATH, forwardProtocol, machineRequest, protocolHeaders, protocolKind,
} from "../lib/identity/protocol-proxy";

afterEach(() => vi.unstubAllEnvs());

const CONSOLE = "http://localhost:3000";
const SESSION = "__Host-boss_session=sess; __Host-boss_preauth=pre; other=keep-home";

function mw(path: string, init: { method?: string; headers?: Record<string, string> } = {}) {
  return middleware(new NextRequest(`${CONSOLE}${path}`, {
    method: init.method ?? "GET", headers: { host: "localhost:3000", ...(init.headers ?? {}) },
  }));
}

function passed(r: Response): boolean {
  return r.status === 200 && r.headers.get("x-middleware-next") === "1";
}

describe("which requests are protocol requests", () => {
  it("SCIM is every /scim/v2 path, in its raw AND normalised spelling", () => {
    expect(protocolKind("GET", "/scim/v2/Users")).toBe("scim");
    expect(protocolKind("PATCH", "/scim/v2/Users/usr_1")).toBe("scim");
    expect(protocolKind("GET", "/scim/v2/boss-dogfood/Groups")).toBe("scim");
    expect(protocolKind("GET", "/scim/v2/%2e%2e/v1/identity/users")).toBeNull();
    expect(protocolKind("GET", "/scim/v2x/Users")).toBeNull();
    expect(protocolKind("GET", "/v1/scim/v2/Users")).toBeNull();
  });

  it("SAML is exactly the metadata GET and the ACS POST", () => {
    expect(protocolKind("GET", "/saml/v2/metadata")).toBe("saml-metadata");
    expect(protocolKind("POST", "/saml/v2/acs")).toBe("saml-acs");
    expect(protocolKind("POST", "/saml/v2/metadata")).toBeNull();
    expect(protocolKind("GET", "/saml/v2/acs")).toBeNull();
    for (const odd of ["/saml/v2/acs/", "/saml/v2//acs", "/saml/v2/%61cs", "/SAML/v2/acs", "/saml/v2/x/../acs", "/saml/v2/p1/acs"]) {
      expect(protocolKind("POST", odd), odd).toBeNull();
    }
  });

  it("a machine request carries no Origin and no Sec-Fetch-*", () => {
    expect(machineRequest(new Headers({ authorization: "Bearer k" }))).toBe(true);
    expect(machineRequest(new Headers({ origin: "https://evil.example" }))).toBe(false);
    expect(machineRequest(new Headers({ "sec-fetch-site": "same-origin" }))).toBe(false);
    expect(machineRequest(new Headers({ "sec-fetch-mode": "no-cors" }))).toBe(false);
  });
});

describe("middleware", () => {
  for (const mode of ["", "network"]) {
    it(`${mode || "password"} console: SCIM from a server passes without a session, a browser's is refused`, async () => {
      vi.stubEnv("BOSS_CONSOLE_AUTH", mode);
      vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "");
      for (const method of ["GET", "POST", "PATCH", "PUT", "DELETE"]) {
        expect(passed(await mw("/scim/v2/Users", { method, headers: { authorization: "Bearer boss_scim_x" } })), method).toBe(true);
      }
      const browser = await mw("/scim/v2/Users", { method: "POST", headers: { origin: CONSOLE, cookie: SESSION, "sec-fetch-site": "same-origin" } });
      expect(browser.status).toBe(403);
      const crossSite = await mw("/scim/v2/Users", { method: "POST", headers: { origin: "https://evil.example" } });
      expect(crossSite.status).toBe(403);
    });
  }

  it("the SAML metadata is public; the ACS POST passes cross-site — and nothing else does", async () => {
    vi.stubEnv("BOSS_CONSOLE_AUTH", "");
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "");
    expect(passed(await mw("/saml/v2/metadata?provider_id=p1"))).toBe(true);
    const idp = { origin: "https://idp.example", "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate",
      "content-type": "application/x-www-form-urlencoded" };
    expect(passed(await mw(SAML_ACS_PATH, { method: "POST", headers: idp }))).toBe(true);
    // The same cross-site POST anywhere else is still refused (with a session, as with none).
    for (const p of ["/v1/auth/login", "/v1/identity/users", "/saml/v2/metadata", "/saml/v2/acs/", "/saml/v2/p1/acs"]) {
      const r = await mw(p, { method: "POST", headers: { ...idp, cookie: SESSION } });
      expect(r.status, p).toBe(403);
    }
    // A sessionless visitor to any other /saml path is sent to sign in, like any page.
    const other = await mw("/saml/v2/other");
    expect(passed(other)).toBe(false);
  });
});

describe("what reaches the API", () => {
  function captured() {
    const calls: { url: string; init: RequestInit }[] = [];
    const fetchImpl = vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      const h = new Headers({ "content-type": "application/scim+json", location: `${CONSOLE}/?signed_in=1` });
      h.append("set-cookie", "__Host-boss_session=new; Path=/; Secure; HttpOnly");
      return new Response("{}", { status: 303, headers: h });
    });
    return { calls, fetchImpl };
  }
  const hostile = {
    cookie: SESSION, "x-boss-csrf": "csrf-token", "x-boss-console-auth": "network", "x-user-id": "usr_owner",
    "x-forwarded-user": "owner", "tailscale-user-login": "owner@example", "proxy-authorization": "Basic x",
  };

  it("SCIM: the bearer, the body and the query — no cookie, CSRF, Origin or identity header — and no Set-Cookie back", async () => {
    const { calls, fetchImpl } = captured();
    const req = new Request(`${CONSOLE}/scim/v2/Users?filter=userName%20eq%20%22a%22`, {
      method: "POST", headers: { authorization: "Bearer boss_scim_k", "content-type": "application/scim+json", ...hostile },
      body: JSON.stringify({ userName: "a@x.test" }),
    });
    const res = await forwardProtocol(req, { apiUrl: "http://api:8000", fetchImpl });
    expect(calls[0].url).toBe("http://api:8000/scim/v2/Users?filter=userName%20eq%20%22a%22");
    const sent = new Headers(calls[0].init.headers);
    expect(sent.get("authorization")).toBe("Bearer boss_scim_k");
    for (const h of ["cookie", "x-boss-csrf", "origin", "x-boss-console-auth", "x-user-id", "x-forwarded-user",
      "tailscale-user-login", "proxy-authorization", "sec-fetch-site"]) {
      expect(sent.get(h), h).toBeNull();
    }
    expect(new TextDecoder().decode(calls[0].init.body as Uint8Array)).toContain("a@x.test");
    expect(res.headers.get("set-cookie")).toBeNull();
  });

  it("SCIM: a browser request is refused before anything is sent", async () => {
    const { calls, fetchImpl } = captured();
    const res = await forwardProtocol(new Request(`${CONSOLE}/scim/v2/Users`, {
      headers: { authorization: "Bearer k", "sec-fetch-site": "same-origin", cookie: SESSION } }), { apiUrl: "http://api:8000", fetchImpl });
    expect(res.status).toBe(403);
    expect(calls).toHaveLength(0);
  });

  it("SAML ACS: the identity cookies and the form, no Authorization or CSRF; the API's redirect and session cookie come back", async () => {
    const { calls, fetchImpl } = captured();
    const req = new Request(`${CONSOLE}${SAML_ACS_PATH}`, {
      method: "POST", headers: { "content-type": "application/x-www-form-urlencoded", authorization: "Bearer stolen", ...hostile },
      body: "SAMLResponse=PHNhbWw%2B&RelayState=r",
    });
    const res = await forwardProtocol(req, { apiUrl: "http://api:8000", fetchImpl });
    expect(calls[0].url).toBe("http://api:8000/saml/v2/acs");
    expect(calls[0].init.redirect).toBe("manual");
    const sent = new Headers(calls[0].init.headers);
    expect(sent.get("cookie")).toBe("__Host-boss_session=sess; __Host-boss_preauth=pre");
    for (const h of ["authorization", "x-boss-csrf", "x-boss-console-auth", "x-user-id", "origin"]) expect(sent.get(h), h).toBeNull();
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toBe(`${CONSOLE}/?signed_in=1`);
    expect(res.headers.get("set-cookie")).toContain("__Host-boss_session=new");
  });

  it("SAML metadata: nothing but the query goes up", async () => {
    const { calls, fetchImpl } = captured();
    await forwardProtocol(new Request(`${CONSOLE}/saml/v2/metadata?provider_id=p1`, { headers: hostile }), { apiUrl: "http://api:8000", fetchImpl });
    expect(calls[0].url).toBe("http://api:8000/saml/v2/metadata?provider_id=p1");
    const sent = new Headers(calls[0].init.headers);
    expect(sent.get("cookie")).toBeNull();
    expect(sent.get("authorization")).toBeNull();
  });

  it("a body over the cap is refused, unsent", async () => {
    const { calls, fetchImpl } = captured();
    const res = await forwardProtocol(new Request(`${CONSOLE}${SAML_ACS_PATH}`, {
      method: "POST", headers: { "content-length": String(2 * 1024 * 1024) }, body: "x" }), { apiUrl: "http://api:8000", fetchImpl });
    expect(res.status).toBe(413);
    expect(calls).toHaveLength(0);
  });

  it("only SCIM's headers carry Authorization; only the ACS's carry a cookie", () => {
    const h = new Headers({ authorization: "Bearer k", cookie: SESSION });
    expect(protocolHeaders("scim", h).get("cookie")).toBeNull();
    expect(protocolHeaders("saml-acs", h).get("authorization")).toBeNull();
    expect(protocolHeaders("saml-metadata", h).get("authorization")).toBeNull();
    expect(protocolHeaders("saml-metadata", h).get("cookie")).toBeNull();
  });
});

describe("the route files", () => {
  const read = (p: string) => readFileSync(new URL(p, import.meta.url), "utf8");
  it("exist, and every one forwards through forwardProtocol (never the /v1 proxy's person-forwarding)", () => {
    for (const p of ["../app/saml/v2/metadata/route.ts", "../app/saml/v2/acs/route.ts", "../app/scim/v2/[...path]/route.ts"]) {
      expect(existsSync(new URL(p, import.meta.url)), p).toBe(true);
      expect(read(p), p).toMatch(/forwardProtocol\(req/);
    }
    // The ACS route answers POST only.
    expect(read("../app/saml/v2/acs/route.ts")).not.toMatch(/export const (GET|PUT|PATCH|DELETE)/);
  });

  it("the protocol calls bypass the server-side wrappers that would add the person back", () => {
    expect(read("../lib/identity/protocol-proxy.ts")).toMatch(/from "\.\.\/direct-fetch"/);
    expect(read("../lib/direct-fetch.ts")).toMatch(/__bossOriginalFetch/);
    expect(read("../lib/feed-cache.ts")).toMatch(/__bossOriginalFetch \?\?= original/);
  });
});
