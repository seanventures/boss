// The login-oauth routes for an Express app:
//   GET  /auth/providers           the configured providers, for the login buttons
//   GET  /auth/login/:provider     redirect to the provider (state + PKCE S256 + nonce)
//   GET  /auth/callback/:provider  check state, exchange the code, check the ID token, sign in
//   POST /auth/logout              CSRF-checked; destroys the session
//   GET  /api/me                   the signed-in user (401 when signed out)
//   GET  /api/admin                an example route only the `admin` role may call
// plus the middleware requireUser and requireRole(role) to protect the app's own routes.
import { randomBytes, timingSafeEqual } from "node:crypto";
import express from "express";
import session from "express-session";
import * as oidc from "openid-client";

import { Providers } from "./providers.mjs";
import { AccountExistsError, UserStore } from "./users.mjs";

// Only a path on this app may be a post-login destination (no open redirect).
function safeReturnTo(value) {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) {
    return "/";
  }
  return value;
}

function sameToken(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  const x = Buffer.from(a);
  const y = Buffer.from(b);
  return x.length === y.length && timingSafeEqual(x, y);
}

export function createAuth(config, { store = new UserStore({ adminEmails: config.adminEmails }), sessionStore } = {}) {
  const providers = new Providers(config);

  // A server-side session (the cookie holds only a signed id). HttpOnly always; Secure whenever
  // the app is served over https; SameSite=Lax, which still sends the cookie on the provider's
  // top-level redirect back to /auth/callback (Strict would drop it and every login would fail
  // the state check). The default MemoryStore is for one process; give a shared store
  // (connect-redis, connect-pg-simple, ...) when the app runs more than one.
  const sessionMiddleware = session({
    name: "sid",
    secret: config.sessionSecret,
    store: sessionStore,
    resave: false,
    saveUninitialized: false,
    // Secure cookies behind a TLS-terminating proxy need the app's `trust proxy` setting
    // (server.mjs trusts a proxy on loopback only).
    cookie: {
      httpOnly: true,
      secure: config.secureCookies,
      sameSite: "lax",
      path: "/",
      maxAge: 7 * 24 * 60 * 60 * 1000,
    },
  });

  function currentUser(req) {
    const id = req.session && req.session.userId;
    return id ? store.get(id) : null;
  }

  function requireUser(req, res, next) {
    const user = currentUser(req);
    if (!user) return res.status(401).json({ error: "signed_out" });
    req.user = user;
    req.roles = store.rolesFor(user);
    return next();
  }

  function requireRole(role) {
    return (req, res, next) =>
      requireUser(req, res, () => {
        if (!req.roles.includes(role)) return res.status(403).json({ error: "forbidden" });
        return next();
      });
  }

  // State-changing requests made with the session cookie must carry the CSRF token /api/me
  // hands out (same-origin JavaScript can read it; another site cannot).
  function requireCsrf(req, res, next) {
    const expected = req.session && req.session.csrf;
    if (!expected || !sameToken(req.get("x-csrf-token"), expected)) {
      return res.status(403).json({ error: "csrf" });
    }
    return next();
  }

  const router = express.Router();
  router.use(sessionMiddleware);

  router.get("/auth/providers", (req, res) => res.json({ providers: providers.list() }));

  router.get("/auth/login/:provider", async (req, res, next) => {
    const id = req.params.provider;
    if (!providers.has(id)) return res.status(404).json({ error: "unknown_provider" });
    try {
      const configuration = await providers.get(id);
      const spec = providers.spec(id);
      const codeVerifier = oidc.randomPKCECodeVerifier();
      const state = oidc.randomState();
      const nonce = spec.kind === "oidc" ? oidc.randomNonce() : undefined;
      const params = {
        redirect_uri: `${config.baseUrl}/auth/callback/${id}`,
        scope: spec.scope,
        state,
        code_challenge: await oidc.calculatePKCECodeChallenge(codeVerifier),
        code_challenge_method: "S256",
      };
      if (nonce) params.nonce = nonce;
      req.session.oauth = { provider: id, state, nonce, codeVerifier, returnTo: safeReturnTo(req.query.return_to) };
      const url = oidc.buildAuthorizationUrl(configuration, params);
      req.session.save((err) => (err ? next(err) : res.redirect(302, url.href)));
    } catch (err) {
      res.status(502).json({ error: "provider_unavailable" });
    }
  });

  router.get("/auth/callback/:provider", async (req, res, next) => {
    const id = req.params.provider;
    if (!providers.has(id)) return res.status(404).json({ error: "unknown_provider" });
    const pending = req.session.oauth;
    delete req.session.oauth; // one use, whatever happens next
    if (!pending || pending.provider !== id || !sameToken(String(req.query.state || ""), pending.state)) {
      return res.status(400).json({ error: "invalid_state" });
    }
    if (req.query.error) return res.status(400).json({ error: "provider_refused" });
    let profile;
    try {
      const configuration = await providers.get(id);
      const callbackUrl = new URL(req.originalUrl, config.baseUrl);
      const tokens = await oidc.authorizationCodeGrant(configuration, callbackUrl, {
        pkceCodeVerifier: pending.codeVerifier,
        expectedState: pending.state,
        expectedNonce: pending.nonce,
        idTokenExpected: Boolean(pending.nonce),
      });
      profile = await providers.profile(id, tokens);
    } catch (err) {
      return res.status(400).json({ error: "login_failed" });
    }
    let user;
    try {
      user = store.signIn({ provider: id, ...profile });
    } catch (err) {
      if (err instanceof AccountExistsError) return res.status(409).json({ error: err.code });
      return next(err);
    }
    // A fresh session id at sign-in (no session fixation), and a fresh CSRF token.
    req.session.regenerate((err) => {
      if (err) return next(err);
      req.session.userId = user.id;
      req.session.csrf = randomBytes(32).toString("base64url");
      req.session.save((err2) => (err2 ? next(err2) : res.redirect(302, pending.returnTo)));
    });
  });

  router.post("/auth/logout", requireCsrf, (req, res, next) => {
    req.session.destroy((err) => {
      if (err) return next(err);
      res.clearCookie("sid", { path: "/", httpOnly: true, secure: config.secureCookies, sameSite: "lax" });
      res.status(204).end();
    });
  });

  router.get("/api/me", requireUser, (req, res) => {
    const u = req.user;
    res.json({
      id: u.id,
      email: u.email,
      email_verified: u.emailVerified,
      name: u.name,
      avatar: u.avatar,
      providers: u.providers,
      roles: req.roles,
      csrf_token: req.session.csrf,
    });
  });

  router.get("/api/admin", requireRole("admin"), (req, res) => res.json({ ok: true }));

  return { router, requireUser, requireRole, requireCsrf, sessionMiddleware, store, currentUser };
}
