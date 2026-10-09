// F6 end-to-end proof, customer pool: an openid-client RP; the "browser" is fetch with a cookie
// jar. Sign up on BOSS's hosted pages, enrol TOTP, consent, get tokens (pairwise sub), userinfo,
// refresh; then sign in again (prompt=login) with password + TOTP; then revoke the app on the
// hosted account page -> the app's next refresh fails.
//   node rp-customer.mjs <world.json> <boss_origin> <rp_origin>
import { createHmac, randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import * as client from "openid-client";

const [worldPath, boss, rp] = process.argv.slice(2);
const world = JSON.parse(readFileSync(worldPath, "utf8"));
const issuer = new URL(`${boss}/oidc/${world.org}`);
const ok = (cond, what) => { if (!cond) { console.error(`FAIL: ${what}`); process.exit(1); } console.log(`ok  ${what}`); };

const jar = new Map();
async function browse(url, init = {}) {
  const headers = { ...(init.headers ?? {}), cookie: [...jar].map(([k, v]) => `${k}=${v}`).join("; ") };
  if (init.method === "POST") Object.assign(headers, { origin: boss, "sec-fetch-site": "same-origin" });
  const r = await fetch(new URL(url, boss), { ...init, headers, redirect: "manual" });
  for (const c of r.headers.getSetCookie()) {
    const [pair, ...attrs] = c.split(";");
    const [k, v] = pair.split("=");
    if (attrs.some((a) => a.trim().toLowerCase() === "max-age=0") || !v) jar.delete(k.trim()); else jar.set(k.trim(), v.trim());
  }
  return r;
}
const form = (path, data) => browse(path, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: new URLSearchParams(data) });
const field = (html, n) => (html.match(new RegExp(`name="${n}" value="([^"]*)"`)) ?? [])[1];
function totp(secret, offset = 0) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = ""; for (const ch of secret) bits += alphabet.indexOf(ch).toString(2).padStart(5, "0");
  const key = Buffer.from(bits.match(/.{8}/g).map((b) => parseInt(b, 2)));
  const counter = Buffer.alloc(8); counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30000) + offset));
  const mac = createHmac("sha1", key).update(counter).digest();
  const o = mac[mac.length - 1] & 15;
  return String((mac.readUInt32BE(o) & 0x7fffffff) % 1e6).padStart(6, "0");
}

const insecure = boss.startsWith("http://") ? { execute: [client.allowInsecureRequests] } : {};
const config = await client.discovery(issuer, world.client_id, undefined, client.ClientSecretBasic(world.client_secret), insecure);

async function authorize(extra = {}) {
  const verifier = client.randomPKCECodeVerifier(), state = client.randomState(), nonce = client.randomNonce();
  const url = client.buildAuthorizationUrl(config, { redirect_uri: `${rp}/callback`, scope: "openid profile email roles offline_access",
    state, nonce, code_challenge: await client.calculatePKCECodeChallenge(verifier), code_challenge_method: "S256", ...extra });
  return { r: await browse(url), verifier, state, nonce };
}
async function exchange(loc, a) {
  const cb = new URL(loc);
  ok(cb.href.startsWith(`${rp}/callback?`) && cb.searchParams.get("iss") === issuer.href, "callback carries code, state and iss");
  return client.authorizationCodeGrant(config, cb, { pkceCodeVerifier: a.verifier, expectedState: a.state, expectedNonce: a.nonce, idTokenExpected: true });
}

// sign up -> TOTP enrolment -> consent -> tokens
const email = `cust-${randomBytes(4).toString("hex")}@example.test`, password = "violet-otter-harbour-1987";
let a = await authorize();
let page = await a.r.text();
ok(a.r.status === 200 && page.includes("Sign in to Bank"), "authorize shows BOSS's hosted sign-in for the app");
const interaction = field(page, "interaction");
page = await (await browse(`/oidc/${world.org}/account?view=signup&interaction=${interaction}`)).text();
ok(page.includes("Create your Bank account"), "sign-up page");
let r = await form(`/oidc/${world.org}/hosted/signup`, { interaction, name: "Cara Customer", email, password });
page = await r.text();
ok(r.status === 200 && page.includes("authenticator app"), "signed up; MFA is required: TOTP enrolment");
const secret = page.match(/<code>([A-Z2-7]+)<\/code>/)[1];
r = await form(`/oidc/${world.org}/hosted/enrol-totp`, { interaction, code: totp(secret) });
page = await r.text();
ok(r.status === 200 && page.includes("Bank wants to sign you in") && jar.has("__Host-boss_app_session"), "TOTP enrolled; app session set; consent page");
r = await form(`/oidc/${world.org}/consent`, { interaction: field(page, "interaction"), csrf: field(page, "csrf"), decision: "allow" });
ok(r.status === 303, "consent allowed");
let tokens = await exchange(r.headers.get("location"), a);
const sub = tokens.claims().sub;
ok(tokens.claims().acr === "urn:boss:acr:mfa" && !sub.startsWith("opu_") && tokens.refresh_token, "ID token verified: MFA acr, pairwise sub, refresh token");
const info = await client.fetchUserInfo(config, tokens.access_token, sub);
ok(info.email === email && info.name === "Cara Customer" && Array.isArray(info.roles), `userinfo: ${info.email}`);
tokens = await client.refreshTokenGrant(config, tokens.refresh_token);
ok(tokens.claims().sub === sub, "refresh rotated, same subject");

// prompt=login: password, then the next TOTP code
a = await authorize({ prompt: "login" });
page = await a.r.text();
const i2 = field(page, "interaction");
r = await form(`/oidc/${world.org}/hosted/signin`, { interaction: i2, email, password });
ok((await r.text()).includes("Second step"), "prompt=login: password accepted, second step asked");
r = await form(`/oidc/${world.org}/hosted/mfa`, { interaction: i2, code: totp(secret, 1) });
ok(r.status === 303, "TOTP accepted");
const again = await exchange(r.headers.get("location"), a);
ok(again.claims().sub === sub, "same pairwise subject on the second sign-in");

// the account page: revoke the app -> its refresh token stops working
page = await (await browse(`/oidc/${world.org}/account?pool=${world.pool}`)).text();
ok(page.includes("Connected apps") && page.includes("Bank"), "hosted account page lists the connected app");
r = await form(`/oidc/${world.org}/hosted/account`, { pool: world.pool, action: "revoke", client_id: world.client_id, csrf: field(page, "csrf") });
ok((await r.text()).includes("disconnected"), "app disconnected on the account page");
let refused;
try { await client.refreshTokenGrant(config, again.refresh_token); refused = "accepted"; } catch (e) { refused = e.error ?? "refused"; }
ok(refused === "invalid_grant", "the app's next refresh fails (invalid_grant)");
console.log("E2E CUSTOMER PASS");
