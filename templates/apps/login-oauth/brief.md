# Add login with Google, GitHub or BOSS

Add sign-in to this app with OAuth 2.0 / OpenID Connect providers, server-side sessions,
sign-out, roles and a protected route. Follow the reference implementation for this app's
stack; adapt it to the app's router, data layer and UI rather than copying files over the top
of the app's own.

## The contract (the template's tests check exactly this, over HTTP)

| Route | Behaviour |
|---|---|
| `GET /auth/providers` | `{"providers": [{"id", "name", "login_url"}]}`: the providers whose client ID is set |
| `GET /auth/login/:provider` | 302 to the provider with `state`, PKCE `code_challenge` (S256) and, for OIDC, `nonce`; `?return_to=/path` (a local path only) is where the user lands after; 404 for an unknown provider |
| `GET /auth/callback/:provider` | 400 `invalid_state` unless `state` matches the one this browser's session started (one use); 400 `login_failed` when the code exchange or the ID token check (signature, issuer, audience, expiry, nonce) fails; 409 `account_exists_unverified_email` (see linking); else a NEW session id and a 302 to `return_to` |
| `POST /auth/logout` | 403 `csrf` without the right `X-CSRF-Token` header; else destroys the session, 204 |
| `GET /api/me` | 401 `signed_out`; else `{id, email, email_verified, name, avatar, providers, roles, csrf_token}` |
| `GET /api/admin` | 401 signed out, 403 without the `admin` role, else `{"ok": true}` |

- **Providers**: `google` (OIDC, issuer `GOOGLE_ISSUER`), `github` (OAuth 2.0 at `GITHUB_URL`,
  profile and verified primary email from `GITHUB_API_URL`), `boss` (any OIDC issuer with
  discovery at `OIDC_BOSS_ISSUER`: "Sign in with BOSS"). A provider is on when its client ID is
  set. The redirect URI is always `APP_BASE_URL` + `/auth/callback/<provider>`.
- **Users**: tables `users` and `user_identities` (`schema.sql`), keyed by (provider, subject).
  Linking is `verified_email_only`: a new identity joins an existing account with the same email
  only when both have the email verified; otherwise 409.
- **Roles**: `user` for everyone; `admin` when the verified email is in `AUTH_ADMIN_EMAILS`.
  Protect the app's own routes with `requireUser` / `requireRole("admin")` (or the stack's
  equivalent below).
- **Session cookie**: `HttpOnly`, `SameSite=Lax`, `Secure` when `APP_BASE_URL` is https.
- **UI**: login buttons (from `/auth/providers`, full-page navigation) and a signed-in header
  with the name and a Sign out button (POST with the CSRF token from `/api/me`).
- **Secrets** (`GOOGLE_CLIENT_ID/SECRET`, `GITHUB_CLIENT_ID/SECRET`, `SESSION_SECRET`,
  `OIDC_BOSS_CLIENT_ID/SECRET`) come from the environment at run time. Never write a value into
  the code, a config file, `.env` or a test.

## Per stack

- **node** (Express): `auth/config.mjs`, `auth/providers.mjs` (openid-client), `auth/users.mjs`,
  `auth/routes.mjs` (`createAuth(config)` returns the router and the middleware), `server.mjs`
  (how to mount it), `ui/LoginButtons.jsx` (React). Add `express-session` and `openid-client`
  at the versions in `package.json` to the app's dependencies, and add its two scripts
  `pretest:login-oauth` and `test:login-oauth` to the app's `package.json` unchanged. If the app
  is not ESM, keep these files as `.mjs` and load them with `import()`. Copy
  `tests/template_login_oauth/` (the tests and the fake provider) to the same path.
- **python** (FastAPI): the `auth/` package (`config.py`, `users.py`, `routes.py` with
  `create_auth(config)` returning the router, the session middleware settings and the
  `require_user` / `require_role` dependencies) and `main.py` (how to mount it). Add the lines of
  `requirements.txt` to the app's requirements. Copy `tests/template_login_oauth/` (the tests,
  `conftest.py` and the fake provider) to the same path.

## The tests are the proof

Keep the template's tests as they are. They sign in through an in-process fake provider (no
network) and check the round trip, state, PKCE, nonce, linking, the cookie flags, CSRF on
sign-out, `/api/me` and the admin route.
