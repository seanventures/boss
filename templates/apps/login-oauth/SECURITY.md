# Login with Google, GitHub or BOSS: security note

## What it protects

Sign-in and the session. It establishes WHO the person is (as the provider vouches for them)
and keeps them signed in. It adds one authorization rule, "signed in" (`requireUser`), and one
example role check (`admin`, from `AUTH_ADMIN_EMAILS`). Anything finer, such as which records
a user may read or change, is the app's job; with BOSS's OIDC provider a `roles` claim can
carry roles, but the app must still check them on every route.

## How the sign-in is protected

- **state**: a fresh random value per login attempt, kept in the server-side session and
  compared in constant time on the callback, then discarded whatever the outcome. A callback
  that does not match it (forged, replayed, or arriving in a browser that never started the
  login, which is login CSRF) is refused with 400 and signs nobody in.
- **PKCE (S256)** on every provider, including the confidential ones: the code verifier never
  leaves the server; the provider only sees its SHA-256 challenge. A stolen authorization code
  cannot be redeemed without it.
- **nonce** (Google, BOSS): a fresh random value bound into the ID token; a token with another
  nonce is refused. The ID token's signature (RS256, keys from the issuer's JWKS), issuer,
  audience and expiry are checked by the library (openid-client, Authlib, Spring Security).
- **Redirect URI**: fixed per provider as `APP_BASE_URL/auth/callback/<provider>`, never taken
  from the request. The post-login destination (`return_to`) is accepted only as a path on this
  app, so the login is not an open redirect.
- **Account linking is `verified_email_only`**: a new provider joins an existing account only
  when BOTH the new provider and the existing account have that email verified. An unverified
  email that matches an account is refused (409), so an attacker who registers someone's
  address, unverified, at some provider cannot get into, or pre-create, their account.
  Identities are keyed by (provider, subject), never by email alone.

## The session

- Server-side session; the cookie holds only an id, signed with `SESSION_SECRET`.
- Cookie flags: `HttpOnly` (no script access), `SameSite=Lax`, `Secure` whenever `APP_BASE_URL`
  is https, `Path=/`, 7 days. `Lax`, not `Strict`: the provider's redirect back is a cross-site
  top-level navigation, and a `Strict` cookie would be dropped there, failing every login.
- The session id is replaced at sign-in (no session fixation) and destroyed at sign-out.
- Behind a TLS-terminating proxy the app must trust that proxy's `X-Forwarded-Proto` (only
  that proxy's), or the Secure cookie is not set: Node trusts loopback by default
  (`TRUST_PROXY`), FastAPI needs uvicorn's `--proxy-headers`, Spring Boot
  `server.forward-headers-strategy`.
- The reference session stores are in memory (one process). Running more than one instance
  needs a shared store (Redis, the app's database).

## CSRF

- Sign-out and any other state-changing request made with the session cookie must carry the
  `X-CSRF-Token` header with the value `/api/me` returns; only same-origin JavaScript can read
  it. A missing or wrong token is 403. `SameSite=Lax` is a second layer, not the only one.
- Sign-in itself is protected by `state` (above).

## Secrets

Client IDs and secrets and `SESSION_SECRET` are app secrets: BOSS keeps them in its vault and
hands them to the app as environment variables at run time. They are never in the code, the
tests (which use a fake provider and values marked `fake`), a log or an error response. Error
responses are fixed codes (`invalid_state`, `login_failed`, ...) and never echo provider
responses or tokens.

## What it does not do

- No password login, no MFA of its own: MFA is the provider's (Google, GitHub, BOSS pools).
- No refresh tokens are kept and no provider API is called after sign-in (GitHub's profile
  is read once at sign-in).
- No rate limiting of the login routes; add it at the proxy or in the app if needed.
- No account deletion or unlinking UI.
