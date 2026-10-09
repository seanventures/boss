// The login-oauth template's own tests, against its HTTP routes (not internals), with an
// in-process fake identity provider: no network. Run: npm run test:login-oauth
import { after, before, beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";

import { loadConfig } from "../../auth/config.mjs";
import { createApp } from "../../server.mjs";
import { startFakeIdp } from "./fake-idp.mjs";

const BASE_URL = "https://app.example.test"; // https, so the session cookie must be Secure
let idp;
let server;
let origin;

function config() {
  return loadConfig({
    APP_BASE_URL: BASE_URL,
    SESSION_SECRET: "fake-session-secret-for-the-template-tests-only",
    AUTH_ADMIN_EMAILS: "boss@example.com",
    GOOGLE_ISSUER: idp.issuer,
    GOOGLE_CLIENT_ID: "fake-google-client",
    GOOGLE_CLIENT_SECRET: "fake-google-secret",
    GITHUB_URL: idp.issuer,
    GITHUB_API_URL: `${idp.issuer}/api`,
    GITHUB_CLIENT_ID: "fake-github-client",
    GITHUB_CLIENT_SECRET: "fake-github-secret",
    OIDC_BOSS_ISSUER: idp.issuer,
    OIDC_BOSS_CLIENT_ID: "fake-boss-client",
    OIDC_BOSS_CLIENT_SECRET: "fake-boss-secret",
  });
}

before(async () => {
  idp = await startFakeIdp();
  idp.clients.set("fake-google-client", "fake-google-secret");
  idp.clients.set("fake-github-client", "fake-github-secret");
  idp.clients.set("fake-boss-client", "fake-boss-secret");
});

beforeEach(async () => {
  if (server) { server.closeAllConnections(); await new Promise((r) => server.close(r)); }
  idp.tamper = {};
  idp.user = { sub: "g-1", email: "ada@example.com", email_verified: true, name: "Ada", picture: null };
  server = createApp(config()).listen(0, "127.0.0.1");
  await new Promise((r) => server.once("listening", r));
  origin = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
  if (server) { server.closeAllConnections(); await new Promise((r) => server.close(r)); }
  await idp.close();
});

// A browser: a cookie jar, manual redirects, and the https the TLS proxy would report.
class Browser {
  constructor() {
    this.jar = new Map();
    this.setCookies = [];
  }

  async get(path, init = {}) {
    return this.request(path, { ...init, method: init.method || "GET" });
  }

  async request(path, init = {}) {
    const headers = { "X-Forwarded-Proto": "https", ...(init.headers || {}) };
    if (this.jar.size) headers.Cookie = [...this.jar].map(([k, v]) => `${k}=${v}`).join("; ");
    const res = await fetch(origin + path, { ...init, headers, redirect: "manual" });
    for (const raw of res.headers.getSetCookie()) {
      this.setCookies.push(raw);
      const [pair, ...attrs] = raw.split(";");
      const [name, value] = pair.split("=");
      const expired = attrs.some((a) => /^\s*expires=Thu, 01 Jan 1970/i.test(a)) || value === "";
      if (expired) this.jar.delete(name.trim());
      else this.jar.set(name.trim(), value);
    }
    return res;
  }

  // Start a login, let the fake provider answer, and return the app's callback URL (path+query).
  async toProvider(provider, query = "") {
    const start = await this.get(`/auth/login/${provider}${query}`);
    assert.equal(start.status, 302, "login redirects to the provider");
    const authorizeUrl = new URL(start.headers.get("location"));
    const atIdp = await fetch(authorizeUrl, { redirect: "manual" });
    assert.equal(atIdp.status, 302, `the provider accepted the authorize request (${atIdp.status})`);
    const back = new URL(atIdp.headers.get("location"));
    assert.equal(back.origin, BASE_URL, "the provider sends the browser back to APP_BASE_URL");
    return { authorizeUrl, callback: back.pathname + back.search };
  }

  async login(provider, query = "") {
    const { callback } = await this.toProvider(provider, query);
    return this.get(callback);
  }
}

test("signed out, /api/me answers 401 and /api/admin 401", async () => {
  const b = new Browser();
  assert.equal((await b.get("/api/me")).status, 401);
  assert.equal((await b.get("/api/admin")).status, 401);
});

test("Google login round trip with the fake provider signs the user in", async () => {
  const b = new Browser();
  const done = await b.login("google");
  assert.equal(done.status, 302);
  assert.equal(done.headers.get("location"), "/");
  const me = await b.get("/api/me");
  assert.equal(me.status, 200);
  const body = await me.json();
  assert.equal(body.email, "ada@example.com");
  assert.deepEqual(body.roles, ["user"]);
  assert.deepEqual(body.providers, ["google"]);
  assert.ok(body.csrf_token);
});

test("GitHub login uses the verified primary email from the GitHub API", async () => {
  idp.user = { sub: "gh-42", email: "grace@example.com", email_verified: true, name: "grace", picture: null };
  const b = new Browser();
  assert.equal((await b.login("github")).status, 302);
  const body = await (await b.get("/api/me")).json();
  assert.equal(body.email, "grace@example.com");
  assert.equal(body.email_verified, true);
  assert.deepEqual(body.providers, ["github"]);
});

test("Sign in with BOSS works through OIDC discovery", async () => {
  const b = new Browser();
  const providers = await (await b.get("/auth/providers")).json();
  assert.deepEqual(providers.providers.map((p) => p.id).sort(), ["boss", "github", "google"]);
  assert.equal((await b.login("boss")).status, 302);
  assert.equal((await b.get("/api/me")).status, 200);
});

test("a callback whose state does not match is rejected and signs nobody in", async () => {
  const b = new Browser();
  const { callback } = await b.toProvider("google");
  const forged = callback.replace(/state=[^&]+/, "state=forged-state");
  const res = await b.get(forged);
  assert.equal(res.status, 400);
  assert.equal((await res.json()).error, "invalid_state");
  assert.equal((await b.get("/api/me")).status, 401);
  // ...and the real state is now spent too: the attempt cannot be replayed.
  assert.equal((await b.get(callback)).status, 400);
});

test("a callback in a browser that never started the login is rejected (login CSRF)", async () => {
  const attacker = new Browser();
  const { callback } = await attacker.toProvider("google");
  const victim = new Browser();
  assert.equal((await victim.get(callback)).status, 400);
  assert.equal((await victim.get("/api/me")).status, 401);
});

test("PKCE S256 is used: the token request's verifier hashes to the authorize challenge", async () => {
  const b = new Browser();
  const { authorizeUrl } = await b.toProvider("google").then(async (r) => {
    await b.get(r.callback);
    return r;
  });
  assert.equal(authorizeUrl.searchParams.get("code_challenge_method"), "S256");
  const verifier = idp.lastToken.code_verifier;
  assert.ok(verifier && verifier.length >= 43);
  const hashed = createHash("sha256").update(verifier).digest("base64url");
  assert.equal(hashed, authorizeUrl.searchParams.get("code_challenge"));
  assert.equal((await b.get("/api/me")).status, 200);
});

test("an ID token whose nonce does not match is rejected", async () => {
  idp.tamper = { nonce: "a-different-nonce" };
  const b = new Browser();
  const res = await b.login("google");
  assert.equal(res.status, 400);
  assert.equal((await res.json()).error, "login_failed");
  assert.equal((await b.get("/api/me")).status, 401);
});

test("an unverified email does not link to an existing account", async () => {
  const first = new Browser();
  assert.equal((await first.login("google")).status, 302); // ada@example.com, verified
  idp.user = { sub: "gh-7", email: "ada@example.com", email_verified: false, name: "mallory", picture: null };
  const attacker = new Browser();
  const res = await attacker.login("github");
  assert.equal(res.status, 409);
  assert.equal((await res.json()).error, "account_exists_unverified_email");
  assert.equal((await attacker.get("/api/me")).status, 401);
});

test("a verified email links a second provider to the same account", async () => {
  const b = new Browser();
  await b.login("google");
  const id = (await (await b.get("/api/me")).json()).id;
  idp.user = { sub: "gh-8", email: "ada@example.com", email_verified: true, name: "ada", picture: null };
  const again = new Browser();
  await again.login("github");
  const me = await (await again.get("/api/me")).json();
  assert.equal(me.id, id);
  assert.deepEqual(me.providers.sort(), ["github", "google"]);
});

test("the session cookie is HttpOnly, Secure, SameSite=Lax, and renewed at sign-in", async () => {
  const b = new Browser();
  await b.get("/auth/login/google");
  const before = b.jar.get("sid");
  const { callback } = await b.toProvider("google");
  await b.get(callback);
  const after = b.jar.get("sid");
  assert.ok(before && after && before !== after, "a new session id after sign-in (no fixation)");
  const cookie = b.setCookies.at(-1);
  assert.match(cookie, /^sid=/);
  assert.match(cookie, /;\s*HttpOnly/i);
  assert.match(cookie, /;\s*Secure/i);
  assert.match(cookie, /;\s*SameSite=Lax/i);
});

test("logout needs the CSRF token, then clears the session", async () => {
  const b = new Browser();
  await b.login("google");
  const { csrf_token: csrf } = await (await b.get("/api/me")).json();
  assert.equal((await b.request("/auth/logout", { method: "POST" })).status, 403);
  assert.equal((await b.request("/auth/logout", { method: "POST", headers: { "X-CSRF-Token": "wrong" } })).status, 403);
  assert.equal((await b.get("/api/me")).status, 200, "a refused logout leaves the session");
  const out = await b.request("/auth/logout", { method: "POST", headers: { "X-CSRF-Token": csrf } });
  assert.equal(out.status, 204);
  assert.equal((await b.get("/api/me")).status, 401);
});

test("the admin route needs the admin role", async () => {
  const user = new Browser();
  await user.login("google");
  assert.equal((await user.get("/api/admin")).status, 403);
  idp.user = { sub: "g-boss", email: "boss@example.com", email_verified: true, name: "Boss", picture: null };
  const admin = new Browser();
  await admin.login("google");
  assert.deepEqual((await (await admin.get("/api/me")).json()).roles, ["user", "admin"]);
  assert.equal((await admin.get("/api/admin")).status, 200);
});

test("return_to only accepts a path on this app (no open redirect)", async () => {
  const ok = new Browser();
  const res = await ok.login("google", "?return_to=/dashboard");
  assert.equal(res.headers.get("location"), "/dashboard");
  for (const evil of ["https://evil.example", "//evil.example", "/\\evil.example"]) {
    const b = new Browser();
    const r = await b.login("google", `?return_to=${encodeURIComponent(evil)}`);
    assert.equal(r.headers.get("location"), "/");
  }
});

test("an unknown provider is a 404", async () => {
  const b = new Browser();
  assert.equal((await b.get("/auth/login/facebook")).status, 404);
  assert.equal((await b.get("/auth/callback/facebook?code=x&state=y")).status, 404);
});
