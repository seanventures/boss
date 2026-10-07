/**
 * The console's identity core: the pass-through's header rules, the session-partitioned feed
 * cache key, the scope-choosing can(), the page table, and the stub API's error contract (the
 * refusals the console must mirror: 401 unauthenticated, 401 step_up_required, 403 forbidden
 * naming the permission, 403 csrf_failed).
 */
import { beforeEach, describe, expect, it } from "vitest";

import { checkWith } from "../lib/identity/check";
import { snapshotFrom } from "../lib/identity/contract";
import {
  hasSessionCookie, identityCookieHeader, isStrippedHeader, parseIdentityCookies, sessionTag, SESSION_COOKIE,
} from "../lib/identity/forward";
import { isPublicConsolePath, pageFor, IDENTITY_PAGES } from "../lib/identity/pages";
import { isPermission } from "../lib/identity/permissions.gen";
import { resetStub, stubHandle, stubRoutes } from "../lib/identity/stub/server";
import { req, signIn } from "./_helpers/identity-stub-session";

describe("the pass-through forwards the person and nothing that names a principal", () => {
  it("strips every identity-asserting header but the CSRF token", () => {
    for (const h of ["Authorization", "Cookie", "X-Boss-Api-Key", "X-Boss-Principal", "x-boss-intent", "X-User-Id",
      "X-Forwarded-User", "Remote-User", "Tailscale-User-Login", "X-Auth-Request-Email", "Cf-Access-Authenticated-User-Email"]) {
      expect(isStrippedHeader(h), h).toBe(true);
    }
    for (const h of ["X-Boss-CSRF", "Content-Type", "Idempotency-Key", "Accept", "Origin", "Sec-Fetch-Site"]) {
      expect(isStrippedHeader(h), h).toBe(false);
    }
  });

  it("forwards only the identity cookies", () => {
    const raw = `${SESSION_COOKIE}=bss_abc; theme=dark; __Host-boss_preauth=pre_1; _ga=GA1; __Host-boss_device=d1; boss_stub_claim=x`;
    expect(identityCookieHeader(raw)).toBe(`${SESSION_COOKIE}=bss_abc; __Host-boss_preauth=pre_1; __Host-boss_device=d1`);
    expect(identityCookieHeader(raw, { stub: true })).toContain("boss_stub_claim=x");
    expect(hasSessionCookie(raw)).toBe(true);
    expect(hasSessionCookie("theme=dark")).toBe(false);
    expect(parseIdentityCookies(raw)[SESSION_COOKIE]).toBe("bss_abc");
  });

  it("partitions cached reads by session, never by URL alone", () => {
    const a = sessionTag(new Headers({ cookie: `${SESSION_COOKIE}=bss_owner` }));
    const b = sessionTag(new Headers({ cookie: `${SESSION_COOKIE}=bss_viewer` }));
    expect(a).not.toBe(b);
    expect(sessionTag(new Headers())).toBe("anon");
    expect(a).not.toContain("bss_owner");
  });
});

describe("can() at the right scope", () => {
  const org = snapshotFrom({ epoch: 1, scope: "org", permissions: ["code_task:read", "user:read"], step_up_fresh_until: null, catalogue_fingerprint: "x" });
  const project = snapshotFrom({ epoch: 1, scope: "project:acme-web", permissions: ["code_task:read", "deployment:production"], step_up_fresh_until: null, catalogue_fingerprint: "x" });
  it("asks the project for a project permission and the org for an org one", () => {
    expect(checkWith(org, project, "acme-web", "deployment:production").allowed).toBe(true);
    expect(checkWith(org, project, null, "deployment:production").allowed).toBe(false);
    expect(checkWith(org, project, "acme-web", "user:read").allowed).toBe(true);
  });
  it("says what is missing and who to ask", () => {
    expect(checkWith(org, null, null, "user:invite").reason).toBe("Needs “Invite people” · ask an Admin");
  });
});

describe("the page table", () => {
  it("declares every page with a catalogue permission or authenticated", () => {
    for (const p of IDENTITY_PAGES) {
      if ("permission" in p.access) expect(isPermission(p.access.permission), p.path).toBe(true);
      expect(p.label.split(" ").length, p.label).toBeLessThanOrEqual(6);
      expect(p.tip.split(" ").length, p.tip).toBeLessThanOrEqual(14);
    }
    expect(pageFor("/identity/people/usr_sam")?.label).toBe("Person");
    expect(pageFor("/identity/agents/delegations/dlg_1")?.label).toBe("Delegation");
    expect(pageFor("/identity/nope")).toBeNull();
  });
  it("leaves only the sign-in pages and API public", () => {
    for (const p of ["/login", "/claim", "/invite", "/signin", "/signin/link", "/v1/auth/invitations/preview", "/v1/auth/invitations/accept", "/reset", "/signup", "/v1/auth/login", "/v1/auth/options", "/v1/auth/sso/idp_okta/start"]) {
      expect(isPublicConsolePath(p), p).toBe(true);
    }
    for (const p of ["/", "/identity", "/account", "/enrol", "/v1/auth/logout", "/v1/auth/step-up", "/v1/identity/me", "/api/skills"]) {
      expect(isPublicConsolePath(p), p).toBe(false);
    }
  });
});

// ---- the stub's error contract --------------------------------------------------------------

describe("the stub API answers with the real error contract", () => {
  beforeEach(() => resetStub());

  it("refuses without a session, and keeps sign-in public", () => {
    expect(stubHandle(req("GET", "/v1/identity/me")).body).toEqual({ error: "unauthenticated" });
    expect(stubHandle(req("GET", "/v1/auth/options")).status).toBe(200);
    expect(stubHandle(req("POST", "/v1/auth/login", { body: { email: "nobody@x.dev", password: "pw" } })).body).toEqual({ error: "invalid_credentials" });
    expect(stubHandle(req("POST", "/v1/auth/login", { body: { email: "sam@acme.dev", password: "wrong" } })).body).toEqual({ error: "invalid_credentials" });
  });

  it("names the missing permission, and asks for step-up only for step-up permissions", () => {
    const viewer = signIn("diego@acme.dev");
    expect(stubHandle(req("GET", "/v1/identity/users", { cookies: viewer.cookies })).body).toEqual({ error: "forbidden", permission: "user:read" });
    const owner = signIn("param@kaappu.org");
    const fresh = stubHandle(req("POST", "/v1/identity/users", { cookies: owner.cookies, csrf: owner.csrf, body: { emails: ["a@b.dev"], roles: ["viewer"] } }));
    expect(fresh.status).toBe(201); // signed in with a passkey just now: fresh
    const priya = signIn("priya@acme.dev");
    expect(stubHandle(req("POST", "/v1/auth/logout", { cookies: priya.cookies, csrf: "nope" })).body).toEqual({ error: "csrf_failed" });
  });

  it("never lets a write through on a read-only principal (J11 at the API seam)", () => {
    const viewer = signIn("diego@acme.dev");
    const writes = stubRoutes().filter((r) => r.method !== "GET" && r.needs !== "public" && r.needs !== "self");
    expect(writes.length).toBeGreaterThan(25);
    for (const r of writes) {
      const path = r.path.replace(/\(\[\^\/\]\+\)/g, "x").replace(/\\/g, "");
      const out = stubHandle(req(r.method, path, { cookies: viewer.cookies, csrf: viewer.csrf, body: {} }));
      expect(out.status, `${r.method} ${path}`).toBeGreaterThanOrEqual(400);
    }
  });

  it("only ever returns a key's secret in the one creating response", () => {
    const owner = signIn("param@kaappu.org");
    const made = stubHandle(req("POST", "/v1/identity/service-accounts/sva_ci_deployer/keys", {
      cookies: owner.cookies, csrf: owner.csrf, body: { name: "t", scopes: ["deployment:read"], expires_days: 90, replaces: "key_7f3k2q", overlap_hours: 1 },
    }));
    expect(made.status).toBe(201);
    const secret = (made.body as { secret: string }).secret;
    expect(secret.startsWith("boss_sak_")).toBe(true);
    const listing = JSON.stringify(stubHandle(req("GET", "/v1/identity/service-accounts/sva_ci_deployer", { cookies: owner.cookies })).body);
    expect(listing).not.toContain(secret.slice(9, 20));
    expect(listing).toContain('"status":"grace"');
  });
});

describe("a network-trusted console says so, and only the console can", () => {
  it("adds X-Boss-Console-Auth: network on its own calls and strips a client's copy", async () => {
    const { withForwardedIdentity, isStrippedHeader: stripped } = await import("../lib/identity/forward");
    expect(stripped("X-Boss-Console-Auth")).toBe(true);
    const prev = process.env.BOSS_CONSOLE_AUTH;
    try {
      delete process.env.BOSS_CONSOLE_AUTH;
      const plain = await withForwardedIdentity("GET", { "X-Boss-Console-Auth": "network" });
      expect(plain.get("x-boss-console-auth")).toBeNull();
      process.env.BOSS_CONSOLE_AUTH = "network";
      const net = await withForwardedIdentity("POST", { "X-Boss-Console-Auth": "spoofed" });
      expect(net.get("x-boss-console-auth")).toBe("network");
    } finally {
      if (prev === undefined) delete process.env.BOSS_CONSOLE_AUTH; else process.env.BOSS_CONSOLE_AUTH = prev;
    }
  });

  it("the /v1 proxy does the same", async () => {
    const { readFileSync } = await import("node:fs");
    const route = readFileSync("app/v1/[...path]/route.ts", "utf8");
    expect(route).toMatch(/consoleAuthMarker\(\)/);
    expect(route).toMatch(/CONSOLE_AUTH_HEADER_LC/);
  });
});
