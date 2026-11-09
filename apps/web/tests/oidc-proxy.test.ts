import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { forwardOidc, isOidcPath, OIDC_BODY_CAP, oidcRequestHeaders, sessionCookieOnly } from "../lib/oidc-proxy";

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

  it("stops a chunked token POST at the cap and does not read the rest", async () => {
    let called = false;
    const fetchImpl = (async () => { called = true; return new Response("{}"); }) as typeof fetch;
    let pulled = 0;
    const chunk = new Uint8Array(16 * 1024).fill(120);
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (pulled >= 200 * 1024) { controller.close(); return; }
        pulled += chunk.byteLength;
        controller.enqueue(chunk);
      },
    });
    const req = new Request("https://boss.test/oidc/o/token", {
      method: "POST", body: stream, duplex: "half",
    } as RequestInit);
    const res = await forwardOidc(req, { apiUrl: "http://api", fetchImpl });
    expect(res.status).toBe(413);
    expect(called).toBe(false);
    expect(pulled).toBeLessThanOrEqual(OIDC_BODY_CAP + 32 * 1024);
  });

  it("forwards a 127.0.0.1 redirect_uri exactly as the app sent it (P1-1)", async () => {
    // NextRequest.url used to rewrite the first loopback address anywhere in the URL to
    // `localhost`. Next 16.4.1 no longer does that, so the rewritten view is pinned on the
    // instance. rawRequestUrl reads Request's own url; a forwarder that reads req.url sends
    // the rewritten redirect_uri and this fails.
    const sent = "http://web:3000/oidc/o/authorize?client_id=c&redirect_uri=http%3A%2F%2F127.0.0.1%3A59999%2Fcb&x=%5B%3A%3A1%5D";
    const req = new NextRequest(sent);
    const rewritten = sent.replace("127.0.0.1", "localhost");
    Object.defineProperty(req, "url", { configurable: true, get: () => rewritten });
    expect(req.url).toBe(rewritten);
    let upstream = "";
    const fetchImpl = (async (u: string) => { upstream = u; return new Response(null, { status: 200 }); }) as unknown as typeof fetch;
    await forwardOidc(req, { apiUrl: "http://api", fetchImpl });
    expect(upstream).toBe("http://api/oidc/o/authorize?client_id=c&redirect_uri=http%3A%2F%2F127.0.0.1%3A59999%2Fcb&x=%5B%3A%3A1%5D");
  });

  it("no server handler reads its URL through NextRequest's normalised url", () => {
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const path = join(dir, name);
        if (statSync(path).isDirectory()) { if (name !== "node_modules") walk(path); continue; }
        if (!/\.(ts|tsx)$/.test(name)) continue;
        const server = /route\.tsx?$/.test(name) || /(proxy|forward)\.ts$/.test(name);
        if (server && /new URL\((req|request)\.url\)/.test(readFileSync(path, "utf8"))) offenders.push(path);
      }
    };
    walk(join(__dirname, "..", "app"));
    walk(join(__dirname, "..", "lib"));
    expect(offenders).toEqual([]);
  });
});
