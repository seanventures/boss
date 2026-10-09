// F6 end-to-end proof: a relying party built on openid-client (a certified OIDC client library)
// signs in through BOSS's workforce pool, gets ID and access tokens, calls userinfo, refreshes,
// and logs out. The "browser" is a fetch with the member's console session cookie and no
// automatic redirects, so every hop is checked.
//
//   node rp.mjs <world.json> <boss_origin> <rp_origin>
import { readFileSync } from "node:fs";
import * as client from "openid-client";

const [worldPath, boss, rp] = process.argv.slice(2);
const world = JSON.parse(readFileSync(worldPath, "utf8"));
const issuer = new URL(`${boss}/oidc/${world.org}`);
const cookie = `__Host-boss_session=${world.session}`;
const ok = (cond, what) => { if (!cond) { console.error(`FAIL: ${what}`); process.exit(1); } console.log(`ok  ${what}`); };

const insecure = boss.startsWith("http://") ? { execute: [client.allowInsecureRequests] } : {};
const config = await client.discovery(issuer, world.client_id, undefined, client.ClientSecretBasic(world.client_secret), insecure);
ok(config.serverMetadata().issuer === issuer.href, "discovery: issuer is the BOSS issuer");

const verifier = client.randomPKCECodeVerifier();
const state = client.randomState();
const nonce = client.randomNonce();
const authUrl = client.buildAuthorizationUrl(config, {
  redirect_uri: `${rp}/callback`, scope: "openid profile email roles offline_access", state, nonce,
  code_challenge: await client.calculatePKCECodeChallenge(verifier), code_challenge_method: "S256",
});

// browser: authorize -> consent page -> allow -> redirect back with code, state and iss
let r = await fetch(authUrl, { headers: { cookie }, redirect: "manual" });
ok(r.status === 200, "authorize shows the consent page to the signed-in member");
const page = await r.text();
const field = (n) => page.match(new RegExp(`name="${n}" value="([^"]+)"`))[1];
const action = page.match(/<form method="post" action="([^"]+)"/)[1];
r = await fetch(new URL(action, boss), {
  method: "POST", redirect: "manual",
  headers: { cookie, origin: boss, "sec-fetch-site": "same-origin", "content-type": "application/x-www-form-urlencoded" },
  body: new URLSearchParams({ interaction: field("interaction"), csrf: field("csrf"), decision: "allow" }),
});
ok(r.status === 303, "consent allowed");
const callback = new URL(r.headers.get("location"));
ok(callback.href.startsWith(`${rp}/callback?`) && callback.searchParams.get("iss") === issuer.href,
  "redirected to the registered callback with iss (RFC 9207)");

// RP: code exchange; openid-client checks state, iss, PKCE, nonce, ID token signature and claims
const tokens = await client.authorizationCodeGrant(config, callback, {
  pkceCodeVerifier: verifier, expectedState: state, expectedNonce: nonce, idTokenExpected: true,
});
const claims = tokens.claims();
ok(!!tokens.access_token && !!tokens.id_token && !!tokens.refresh_token, "ID, access and refresh tokens issued");
ok(claims.aud === world.client_id && claims.nonce === nonce && !claims.sub.startsWith("usr_"),
  "ID token verified by openid-client; pairwise sub, not a BOSS id");

const info = await client.fetchUserInfo(config, tokens.access_token, claims.sub);
ok(info.email?.startsWith("member-") && Array.isArray(info.roles) && info.roles.includes("member"),
  `userinfo: ${info.name} <${info.email}> roles=${JSON.stringify(info.roles)}`);

const refreshed = await client.refreshTokenGrant(config, tokens.refresh_token);
ok(refreshed.refresh_token && refreshed.refresh_token !== tokens.refresh_token, "refresh rotated the refresh token");
ok(refreshed.claims().sub === claims.sub, "refreshed ID token has the same subject");
const before = await client.fetchUserInfo(config, refreshed.access_token, claims.sub);
ok(before.sub === claims.sub, "the refreshed access token works at userinfo");

// logout: RP-initiated, back to the registered post-logout address
const endUrl = client.buildEndSessionUrl(config, {
  id_token_hint: refreshed.id_token, post_logout_redirect_uri: `${rp}/signed-out`, state: "bye",
});
r = await fetch(endUrl, { redirect: "manual" });
ok(r.status === 302 && r.headers.get("location") === `${rp}/signed-out?state=bye`, "end_session returns to the app");
let after;
try { await client.fetchUserInfo(config, refreshed.access_token, claims.sub); after = "accepted"; }
catch (e) { after = e.status ?? e.code ?? "refused"; }
ok(after !== "accepted", `after logout the access token is dead at userinfo (${after})`);
let reuse;
try { await client.refreshTokenGrant(config, tokens.refresh_token); reuse = "accepted"; }
catch (e) { reuse = e.error ?? e.code ?? "refused"; }
ok(reuse === "invalid_grant", "re-using the old refresh token is refused (rotated tokens are single use)");

console.log("E2E PASS");
