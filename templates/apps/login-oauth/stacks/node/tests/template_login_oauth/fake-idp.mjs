// An in-process fake identity provider for the login-oauth tests: no network, no real Google,
// GitHub or BOSS. One server plays all three shapes:
//   OpenID Connect (Google, BOSS): discovery, /authorize, /token (ID token RS256), /jwks, /userinfo
//   GitHub OAuth 2.0:              /login/oauth/authorize, /login/oauth/access_token,
//                                  /api/user, /api/user/emails
// It enforces what a real provider enforces and the app must get right: a registered client
// with the right secret, the exact redirect_uri, one-time codes, and PKCE S256 (the token
// request's code_verifier must hash to the authorize request's code_challenge).
import { createHash, generateKeyPairSync, randomBytes, randomUUID, sign } from "node:crypto";
import http from "node:http";

const b64url = (buf) => Buffer.from(buf).toString("base64url");

export async function startFakeIdp() {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const kid = randomUUID();
  const jwk = { ...publicKey.export({ format: "jwk" }), kid, alg: "RS256", use: "sig" };
  const codes = new Map();
  const tokens = new Map();

  const fake = {
    clients: new Map(), // client id -> secret
    // The person who "signs in" at the provider next.
    user: { sub: "user-1", email: "ada@example.com", email_verified: true, name: "Ada", picture: null },
    // Test knobs: an ID token with a different nonce.
    tamper: {},
    lastAuthorize: null,
    lastToken: null,
    issuer: "",
  };

  function signJwt(payload) {
    const header = b64url(JSON.stringify({ alg: "RS256", typ: "JWT", kid }));
    const body = b64url(JSON.stringify(payload));
    const sig = sign("sha256", Buffer.from(`${header}.${body}`), privateKey);
    return `${header}.${body}.${b64url(sig)}`;
  }

  function json(res, status, body) {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(body));
  }

  function authorize(url, res) {
    const p = Object.fromEntries(url.searchParams);
    fake.lastAuthorize = p;
    if (!fake.clients.has(p.client_id) || !p.redirect_uri || p.response_type !== "code") {
      return json(res, 400, { error: "invalid_request" });
    }
    if (p.code_challenge_method !== "S256" || !p.code_challenge) {
      return json(res, 400, { error: "invalid_request", error_description: "PKCE S256 required" });
    }
    const code = b64url(randomBytes(24));
    codes.set(code, { ...p, user: { ...fake.user } });
    const back = new URL(p.redirect_uri);
    back.searchParams.set("code", code);
    if (p.state !== undefined) back.searchParams.set("state", p.state);
    res.writeHead(302, { Location: back.href });
    res.end();
  }

  function token(req, body, res) {
    const p = Object.fromEntries(new URLSearchParams(body));
    fake.lastToken = p;
    let clientId = p.client_id;
    let secret = p.client_secret;
    const basic = /^Basic (.+)$/.exec(req.headers.authorization || "");
    if (basic) {
      const [id, pw] = Buffer.from(basic[1], "base64").toString().split(":");
      clientId = decodeURIComponent(id);
      secret = decodeURIComponent(pw || "");
    }
    if (!fake.clients.has(clientId) || fake.clients.get(clientId) !== secret) {
      return json(res, 401, { error: "invalid_client" });
    }
    const grant = codes.get(p.code);
    codes.delete(p.code);
    if (p.grant_type !== "authorization_code" || !grant || grant.client_id !== clientId || grant.redirect_uri !== p.redirect_uri) {
      return json(res, 400, { error: "invalid_grant" });
    }
    const challenge = b64url(createHash("sha256").update(p.code_verifier || "").digest());
    if (challenge !== grant.code_challenge) {
      return json(res, 400, { error: "invalid_grant", error_description: "PKCE verification failed" });
    }
    const accessToken = b64url(randomBytes(24));
    tokens.set(accessToken, grant.user);
    const out = { access_token: accessToken, token_type: "Bearer", expires_in: 3600, scope: grant.scope };
    if ((grant.scope || "").split(" ").includes("openid")) {
      const now = Math.floor(Date.now() / 1000);
      out.id_token = signJwt({
        iss: fake.issuer,
        aud: clientId,
        sub: grant.user.sub,
        iat: now,
        exp: now + 300,
        nonce: fake.tamper.nonce ?? grant.nonce,
        email: grant.user.email,
        email_verified: grant.user.email_verified,
        name: grant.user.name,
        picture: grant.user.picture,
      });
    }
    return json(res, 200, out);
  }

  function bearer(req) {
    const m = /^Bearer (.+)$/i.exec(req.headers.authorization || "");
    return m ? tokens.get(m[1]) : undefined;
  }

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, fake.issuer);
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const route = `${req.method} ${url.pathname}`;
      if (route === "GET /.well-known/openid-configuration") {
        return json(res, 200, {
          issuer: fake.issuer,
          authorization_endpoint: `${fake.issuer}/authorize`,
          token_endpoint: `${fake.issuer}/token`,
          userinfo_endpoint: `${fake.issuer}/userinfo`,
          jwks_uri: `${fake.issuer}/jwks`,
          response_types_supported: ["code"],
          subject_types_supported: ["public"],
          id_token_signing_alg_values_supported: ["RS256"],
          code_challenge_methods_supported: ["S256"],
          token_endpoint_auth_methods_supported: ["client_secret_basic", "client_secret_post"],
        });
      }
      if (route === "GET /jwks") return json(res, 200, { keys: [jwk] });
      if (route === "GET /authorize" || route === "GET /login/oauth/authorize") return authorize(url, res);
      if (route === "POST /token" || route === "POST /login/oauth/access_token") return token(req, body, res);
      const who = bearer(req);
      if (route === "GET /userinfo") return who ? json(res, 200, who) : json(res, 401, {});
      if (route === "GET /api/user") {
        return who ? json(res, 200, { id: Number(who.sub.replace(/\D/g, "")) || 1, login: who.name, name: who.name, avatar_url: who.picture }) : json(res, 401, {});
      }
      if (route === "GET /api/user/emails") {
        return who ? json(res, 200, [{ email: who.email, primary: true, verified: who.email_verified }]) : json(res, 401, {});
      }
      return json(res, 404, { error: "not_found" });
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  fake.issuer = `http://127.0.0.1:${server.address().port}`;
  fake.close = () => new Promise((resolve) => server.close(resolve));
  return fake;
}
