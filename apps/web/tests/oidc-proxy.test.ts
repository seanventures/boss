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

  it("relays redirects and page headers, and only the identity cookies of browser steps", async () => {
    const make = () => (async () => {
      const h = new Headers({ location: "https://app.test/cb?code=c", "x-frame-options": "DENY" });
      h.append("set-cookie", "a=b; Path=/");
      h.append("set-cookie", "__Host-boss_app_session=bas_x; Path=/; Secure; HttpOnly; SameSite=Lax");
      return new Response(null, { status: 302, headers: h });
    }) as typeof fetch;
    const r = await forwardOidc(new Request("https://boss.test/oidc/o/authorize?x=1"), { apiUrl: "http://api", fetchImpl: make() });
    expect(r.status).toBe(302);
    expect(r.headers.get("location")).toBe("https://app.test/cb?code=c");
    expect(r.headers.getSetCookie()).toEqual(["__Host-boss_app_session=bas_x; Path=/; Secure; HttpOnly; SameSite=Lax"]);
    expect(r.headers.get("x-frame-options")).toBe("DENY");
    const t = await forwardOidc(new Request("https://boss.test/oidc/o/token", { method: "POST", body: "" }), { apiUrl: "http://api", fetchImpl: make() });
    expect(t.headers.getSetCookie()).toEqual([]);
  });

  it("forwards the app cookies on hosted steps and never on app calls", () => {
    const h = new Headers({ cookie: "__Host-boss_app_session=bas_a; __Host-boss_app_preauth=bap_b; x=1" });
    expect(isOidcPath("/oidc/o/hosted/signin")).toBe(true);
    expect(isOidcPath("/oidc/o/hosted/../token")).toBe(false);
    expect(oidcRequestHeaders("/oidc/o/hosted/signin", h).get("cookie")).toBe("__Host-boss_app_session=bas_a; __Host-boss_app_preauth=bap_b");
    expect(oidcRequestHeaders("/oidc/o/userinfo", h).get("cookie")).toBeNull();
  });

  it("refuses large bodies before calling the API", async () => {
    let called = false;
    const fetchImpl = (async () => { called = true; return new Response("{}"); }) as typeof fetch;
    const big = new Request("https://boss.test/oidc/o/token", { method: "POST", body: "x".repeat(70 * 1024) });
    expect((await forwardOidc(big, { apiUrl: "http://api", fetchImpl })).status).toBe(413);
    expect(called).toBe(false);
  });
});
