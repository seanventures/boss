import { describe, expect, it } from "vitest";

import { forwardOidc, isOidcPath, oidcRequestHeaders, sessionCookieOnly } from "../lib/oidc-proxy";

describe("the OIDC provider on the public origin (F6)", () => {
  it("matches the issuer's endpoints exactly", () => {
    expect(isOidcPath("/oidc/org_x/authorize")).toBe(true);
    expect(isOidcPath("/oidc/org_x/.well-known/openid-configuration")).toBe(true);
    for (const p of ["/oidc/org_x/authorize/", "/oidc/org_x/../v1/x", "/oidc//token", "/oidc/org_x/admin", "/oidcx/o/token"]) {
      expect(isOidcPath(p)).toBe(false);
    }
  });

  it("forwards only the console session cookie, and only on browser steps", () => {
    const h = new Headers({ cookie: "__Host-boss_session=bss_a; __Host-boss_preauth=p; other=1", authorization: "Bearer t",
      origin: "https://boss.test", "sec-fetch-site": "same-origin" });
    const browser = oidcRequestHeaders("/oidc/o/consent", h);
    expect(browser.get("cookie")).toBe("__Host-boss_session=bss_a");
    expect(browser.get("authorization")).toBeNull();
    expect(browser.get("origin")).toBe("https://boss.test");
    const app = oidcRequestHeaders("/oidc/o/token", h);
    expect(app.get("cookie")).toBeNull();
    expect(app.get("authorization")).toBe("Bearer t");
    expect(app.get("origin")).toBeNull();
    expect(oidcRequestHeaders("/oidc/o/jwks", h).get("cookie")).toBeNull();
    expect(sessionCookieOnly("x__Host-boss_session=1")).toBe("");
  });

  it("relays redirects and page headers but never a cookie", async () => {
    const fetchImpl = (async () => new Response(null, { status: 302, headers: {
      location: "https://app.test/cb?code=c", "set-cookie": "a=b", "x-frame-options": "DENY" } })) as typeof fetch;
    const r = await forwardOidc(new Request("https://boss.test/oidc/o/authorize?x=1"), { apiUrl: "http://api", fetchImpl });
    expect(r.status).toBe(302);
    expect(r.headers.get("location")).toBe("https://app.test/cb?code=c");
    expect(r.headers.get("set-cookie")).toBeNull();
    expect(r.headers.get("x-frame-options")).toBe("DENY");
  });

  it("refuses large bodies before calling the API", async () => {
    let called = false;
    const fetchImpl = (async () => { called = true; return new Response("{}"); }) as typeof fetch;
    const big = new Request("https://boss.test/oidc/o/token", { method: "POST", body: "x".repeat(70 * 1024) });
    expect((await forwardOidc(big, { apiUrl: "http://api", fetchImpl })).status).toBe(413);
    expect(called).toBe(false);
  });
});
