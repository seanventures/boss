# BOSS Identity: authentication and authorization

Status: **SPEC, operator decisions recorded (§16, D25–D34); Phase 0 interfaces frozen.** Date: 2026-10-06. Base: main @ b1befb7d.
Build plan, stream ownership and the amendments Phase 0 made: [IDENTITY_BUILD.md](../maintainers/IDENTITY_BUILD.md). The generated catalogue table: [IDENTITY_CATALOGUE.md](IDENTITY_CATALOGUE.md).
Companion: [IDENTITY_PERMISSIONS.md](IDENTITY_PERMISSIONS.md) holds the permission catalogue, the built-in role table, and the route, MCP tool and page mapping. The console mockups are in `~/Downloads/boss-identity-mockup/`.

---

## 1. Summary

BOSS today has one shared console password, one shared API key, and an `actor` string that callers claim in the request body. Capabilities are 21 free strings stored on member rows. About 1,000 routes exist, and roughly 500 of them have no inline guard. OIDC, SAML and SCIM authenticate API bearers but never sign a person in to the console. Agent Passports exist, but no agent holds a token. Anyone who reaches the console can do everything, and the audit trail can only say that "executive_console" did it.

This spec replaces that with an identity system built into BOSS:

1. **Every request has a real principal.** A principal is a person, a service account or an agent. People sign in with a password, a passkey or SSO. MFA is mandatory for Owners and Admins. Machines use per-principal API keys that are hashed, scoped and expiring. Agents use run tokens that live for 5 minutes and are bound to their sandbox.
2. **Every operation declares a permission from one catalogue.** That covers API routes, MCP tools, console pages and console actions. The catalogue lives in code as about 180 `resource:action` entries. It generates the Python enum, the TypeScript union, the DB seed and the built-in roles. A route without a declaration **stops the API from booting**. Tests fail on any undeclared route, page or tool. Access is denied by default.
3. **Agents act for the person who started them, and can only ever narrow.** Effective permission = **person ∩ Passport ceiling ∩ delegation grant ∩ task**, evaluated live at every hop. Chains have a depth cap. Each hop is recorded as RFC 8693 `act` nesting and in an append-only lineage.
4. **External credentials never enter a sandbox.** GitHub and other provider tokens live in an envelope-encrypted vault. A run gets a per-run, down-scoped GitHub App installation token. An egress proxy injects it, so a prompt-injected agent has no token to leak. A long-lived "act for me" is a revocable **delegation grant**, and revoking it cascades to every run, sub-agent and token in under 5 seconds.
5. **Migration never locks anyone out.** The shared password becomes the claim credential for the first Owner. The shared API key becomes a flagged legacy credential bound to a service account. Claimed actors are ignored in favour of the authenticated principal. Network-trusted mode becomes a named, bannered principal. Enforcement runs in **shadow mode** first, logging what it would deny, before it starts denying.

Built in, with no SDK, no plugin framework and no policy language. Tables are plain Postgres. The enforcement point is one function.

---

## 2. Principles and non-goals

**Principles**

- **Fail closed everywhere.** That means no route without a declaration and no fallback to unverified claims. A security dependency that is down never results in "allow". Neither does a stale cache served on error, or a vault that silently falls back to plaintext.
- **Identity comes only from a verified credential.** It never comes from a body field, a query parameter or a client-set header. That rules out `actor`, `x-user-id` and `?accountId=`. The worst identity flaws come from trusting asserted identity.
- **One authority.** The API decides. The console mirrors the API's answers for UX, and is never asked to decide.
- **Narrowing only.** No token, grant, key or role can carry more than the principal that created it holds.
- **Everything revocable, everything attributable.** Every action answers four questions in the UI: who did it, for whom, by which chain, and under which grant.
- **Secrets are never printed, not even in part.** BOSS's existing rule extends to every credential this spec introduces, including key prefixes, which many products show.

**Non-goals (v1)**

- BOSS does not become a general-purpose IdP. It has no SAML IdP role, does not host other apps' logins, and does not broker federation.
- No ReBAC or per-object sharing such as "share this one workflow with Bob". That waits until a customer needs it. It is not bolted onto RBAC.
- No deny rules. Role bindings are a union. Agent intersection narrows, but it is not a deny.
- No nested groups and no role inheritance. Both are easy to build and hard to reason about.
- No OpenFGA and no policy language.

---

## 3. The principal model

| Kind | Id prefix | Signs in to the console | Credentials | Holds access roles | Accountable human |
|---|---|---|---|---|---|
| **Person** (`user`) | `usr_` | Yes, and only people | password (argon2id), TOTP, passkeys, SSO links, personal API keys | yes, directly and through groups | themselves |
| **Service account** (`service_account`) | `sva_` | Never | service-account keys `boss_sak_…` | yes | ≥1 human owner, required |
| **Agent** (`agent`) | `agt_` (the existing Passport id) | Never | none at rest. It receives run tokens per run | yes, for autonomous work only. Always capped by its Passport ceiling | a sponsor, required |
| **System** (`system`) | `sys_` | Never | built-in: `sys_worker`, `sys_collector`, `sys_scim_*`, `sys_host_cli` | fixed, not editable | the Owners |

Every principal row has `status ∈ {pending, active, disabled, suspended, revoked}`.
- `revoked` is terminal. A `reactivate` that undoes revocation is a common flaw; BOSS has none.
- `disabled` applies to people and service accounts. `suspended` applies to agents.
- Every enforcement point checks status live.

**Groups** (`grp_`) are not principals that act. They are holders of role bindings that their members inherit. A group's `source` is `manual`, `scim` or `oidc`. Members of IdP-sourced groups are read-only in BOSS.

**Scopes.** An install has one or more **orgs**. An org has **projects** (today's `projects`, with `workspaces` kept as an internal level). A role binding applies at `org` scope, which covers every project including future ones, or at `project:<id>` scope.

**Agent instances.** An agent principal is the Passport, which is the template. A running agent is an **instance**, `agt_<id>#run_<id>`. Instances have no rows of their own. They exist in run tokens and lineage. This follows Entra's blueprint/instance split: credentials and ceilings belong to the template, and instances only ever hold derived, short-lived tokens.

**Approval seats.** BOSS's governance layer approves risk-tier decisions by *seat* (`ceo`, `cao` and so on, from `default_safety.yaml`). Seats stay, as `approval_seats(seat_key → principal)`. A seat is not a permission. Approving needs **both** `decision:approve` and the seat the tier requires (§6.13).

---

## 4. Authentication

### 4.1 Architecture: the API is the authority

```
Browser ──cookie──► Next.js console ──forwards cookie + CSRF header──► FastAPI /v1 (authority)
                     (no auth logic,                                  ├─ session store (Postgres)
                      pass-through)                                   ├─ PDP: authorize()
Agents in sandboxes ──DPoP run token──► egress proxy / API ──────────┘
CLI / MCP clients ──Bearer boss_pat_… / boss_sak_…──► API
```

- The API sets and reads the session cookie itself. The Next.js `/v1` proxy and the server components forward the browser's cookie unchanged.
- The old `BOSS_CONSOLE_PASSWORD` HMAC cookie and the console's shared `BOSS_API_KEY` both go away (§12).
- Next.js middleware keeps only routing duties: send an unauthenticated visitor to `/login`, and require HTTPS when remote. It decides nothing.
- **One credential per request.** A request carrying both a cookie and a bearer is rejected with 400. That removes any ambiguity about which principal is acting.

### 4.2 Passwords

| Item | Value |
|---|---|
| Hash | **argon2id**, `m=65536 KiB (64 MiB), t=3, p=1`, 16-byte random salt, 32-byte tag, stored as a PHC string (`$argon2id$v=19$m=65536,t=3,p=1$…`). This is RFC 9106's second recommended profile, and takes 60–120 ms on a 2-vCPU VM. |
| Small hosts | `BOSS_ARGON2_PROFILE=small` uses `m=19456, t=2, p=1`, which is the OWASP minimum. The installer chooses it when RAM is under 2 GiB. |
| Memory DoS guard | A process-wide semaphore allows at most 4 hashes in flight. Further requests queue for up to 2 s, then get 429. |
| Upgrade | When a stored hash's parameters are below the current profile, BOSS rehashes it on the next successful sign-in. |
| Length | 12–128 characters, after Unicode NFKC normalisation. There is no silent truncation and no composition rules (NIST 800-63B). |
| Blocklist | A bundled list of the 100k most common passwords, plus context words: "boss", the org name and the user's email local part. It works offline, which matters for self-hosted installs. |
| Breach check | Optional HIBP k-anonymity check with padding. When HIBP is unreachable the password is **accepted, with a posture warning**. The bundled list is the floor, so being offline is never a hole. |
| Pepper | None. The argon2id parameters plus the master-key-encrypted secrets are enough, and a pepper would tie password hashes to master-key rotation. |

Changing or resetting a password revokes every other session, notifies the user, and asks them to review their API keys.

### 4.3 Sessions

| Item | Value |
|---|---|
| Token | `bss_` + 32 random bytes in base64url. Opaque. **Not a JWT.** |
| Storage | `identity_sessions.token_hash = SHA-256(token)`. The raw token exists only in the cookie. |
| Cookie | `__Host-boss_session`, with `Secure; HttpOnly; SameSite=Lax; Path=/` and no `Domain`. |
| Lifetime | Idle 12 h and absolute 7 d by default. Policy ranges are idle 15 min–24 h and absolute 1 h–30 d. |
| Rotation | A new token is issued on sign-in, on MFA completion and on step-up, and the old one is revoked. A pre-auth cookie never becomes the session (no fixation). |
| Server checks per request | Hash lookup (in-process cache ≤5 s, dropped on NOTIFY), then `revoked_at IS NULL`, both expiries, and principal `status = active`. |
| CSRF (cookie-authenticated writes) | All three must hold: `Origin` equals the origin of `BOSS_PUBLIC_URL`; `Sec-Fetch-Site: same-origin` when present; and `X-Boss-CSRF` equals `HMAC(session_id)`, delivered to the console in `/v1/identity/me`. GET handlers must be read-only (§15.4). |
| CORS | There is no CORS. The console is same-origin through its proxy, so the API sends no `Access-Control-Allow-*` headers. |
| Device cookie | `__Host-boss_device` holds a random id, set after a successful sign-in, and is never used as a credential. It exempts that device from the per-account soft lock (§4.10) and drives the "new device" notice. |

Users can see and end their own sessions. Admins with `session:revoke` can end anyone's. A session row records the sign-in methods (`pwd`, `totp`, `passkey`, `sso:<provider>`), `mfa_at`, the IP and a parsed user agent.

### 4.4 MFA: TOTP

- **Parameters:** RFC 6238 with SHA-1, 6 digits and 30 s steps, for compatibility with every authenticator. A window of ±1 step.
- **Storage:** the secret is 20 random bytes, encrypted with AES-256-GCM under the vault KEK, with AAD = `principal_id`. **It is never stored in plaintext**, a common flaw.
- **Replay:** BOSS stores the last accepted time-step and rejects any code at or below it.
- **Attempts:** 5 per challenge, after which the challenge is burned. 20 per hour per user. Failures count toward the account's soft lock.
- **Enrolment:** the user must have authenticated in the last 10 minutes and confirm a valid code. BOSS shows a QR code and the text secret **once**. Removing TOTP requires step-up, and MFA-required roles cannot remove their last factor.

### 4.5 MFA: passkeys (WebAuthn)

- **Implementation:** `py_webauthn`. The RP ID is the host of `BOSS_PUBLIC_URL`. Origins must match exactly. Attestation is `none` (privacy). An optional AAGUID allowlist is available for enterprises.
- **Requirements:** `userVerification: required` and discoverable (resident) credentials. A passkey is a **complete, phishing-resistant sign-in on its own**, so the user enters no password, and it also satisfies MFA after a password.
- **Challenges:** 32 random bytes. BOSS stores their hash in `identity_auth_challenges` with a 5-minute TTL, bound to the pre-auth cookie, single use. During authentication the credential's user handle must map to the principal that the challenge resolves to (an unbound challenge is a common flaw).
- **Sign count:** a counter regression raises a security event and a posture flag. It does not block, because synced passkeys report 0.
- **Limits:** up to 10 passkeys per user. Each can be named and shows when it was last used.

### 4.6 Step-up

- Catalogue entries flagged `step_up` require `session.mfa_at` within the last 10 minutes. Otherwise the API answers `401 {"error":"step_up_required"}`, and the console opens the step-up sheet (passkey first, then TOTP).
- Users without MFA, where policy allows that (Operator and Viewer by default), step up with their password.
- Critical permissions cannot be exercised by a user without MFA. This follows from Owners and Admins being required to enrol.
- Bearer credentials (API keys and run tokens) cannot step up. Step-up permissions therefore **cannot be used by API keys** unless the key's owner is a service account and the permission is in its scopes. This is deliberate: automation can be scoped to those permissions, but a stolen PAT cannot rotate SSO.

### 4.7 Recovery

| Situation | Path | Safeguards |
|---|---|---|
| Forgot password, SMTP configured | Email link: 32 random bytes, stored hashed, 30-minute expiry, single use | The response is identical whether or not the account exists. The link always uses `BOSS_PUBLIC_URL`, never the `Host` header. A reset does not bypass MFA: the user still needs a second factor afterwards. |
| Forgot password, no SMTP | An admin issues a reset link (`user:update`, step-up) | The link is shown once, **to the authenticated admin**, to hand over out of band. Returning secrets from a *public* endpoint is a common flaw. This endpoint is not public. |
| Lost MFA | One of 10 recovery codes: 12 base32 characters, argon2id-hashed (small profile), single use | Using a code notifies the user. Below 3 remaining codes, BOSS forces regeneration. |
| Lost MFA and codes | Admin reset (`user:reset_mfa`, step-up) | Resetting an Owner requires another Owner. The user must re-enrol MFA at the next sign-in. The event is critical-audited and notifies every admin. |
| Sole Owner locked out | Host CLI: `docker exec boss-api boss identity recover-owner --email …` | The CLI needs host shell access. It prints a 15-minute single-use link to stdout. Actor: `sys_host_cli`. Every Owner sees a banner for 7 days. |

Magic-link sign-in and email codes **are offered, only when SMTP is configured**, and built to the rules in §4.14 (D26). Both are easy to get wrong; §4.14 lists each known pitfall and the test that keeps it out. There is **no phone or SMS** method of any kind (SIM-swap risk).

### 4.8 SSO sign-in (OIDC and SAML)

The existing `enterprise_identity_providers`, `self_hosted_identity.py` and SAML ACS now create **console sessions**. The bearer-only behaviour is retired (§12).

**OIDC (BOSS as a relying party)**
- **Configuration:**
  - issuer, client id, and a client secret held in the vault (or `private_key_jwt`);
  - scopes `openid email profile` plus an optional `groups`;
  - an allowed algorithm list (RS256, ES256, EdDSA);
  - a domain allowlist, a JIT mode, group mappings, and "trust IdP MFA";
  - "enforce SSO for these domains".
- **Discovery:** the discovery document's `issuer` must equal the configured issuer **exactly**. Every fetch (discovery, JWKS, token) goes through the **SSRF-safe client** (§4.13).
- **Start:** BOSS generates `state` (32 B), `nonce` (32 B) and a PKCE S256 verifier. It stores them hashed in `identity_auth_challenges`, bound to the pre-auth cookie, for 10 minutes. The redirect URI is fixed at `${BOSS_PUBLIC_URL}/v1/auth/sso/{provider_id}/callback`. It is never taken from the client.
- **Callback checks:**
  - `state` matches and is bound to this browser;
  - the RFC 9207 `iss` parameter matches, when it is present;
  - the code exchange includes the verifier, and there is **no exchange without one**;
  - the id_token signature verifies against the provider's JWKS. The JWKS is cached for 1 h and refetched on an unknown `kid`, at most once a minute. Only configured algorithms are accepted;
  - `iss` matches exactly, `aud` contains the client id, `azp` matches when there are multiple audiences, `exp`, `iat` and `nbf` hold within 60 s of skew, and `nonce` matches.
- **Identity:** the user is identified by **`(iss, sub)`, never by email.** Userinfo is used only for display fields.
- **Presets:** Google, Microsoft and Apple are OIDC presets; GitHub is an OAuth 2.0 preset; all four, and any enterprise provider, appear as icon buttons on the sign-in page (§4.15, §9.7).

**SAML 2.0 (BOSS as a service provider)**
- **Flow:** SP-initiated by default. IdP-initiated sign-in is off by default, because unsolicited responses allow login CSRF.
- **Parsing:** `python3-saml` in strict mode, with `defusedxml` and no DTDs.
- **Signature:** the Assertion must be signed with the configured certificate (key rollover is supported with two certificates). The library's reference checks defend against XML signature wrapping, and a test fixture covers it.
- **Conditions:** Audience equals the SP entity id. Destination and Recipient equal the ACS URL. `InResponseTo` equals the stored request id. Assertions must fall inside `NotBefore`/`NotOnOrAfter` with 60 s of skew. Each assertion id goes into a replay cache until it expires.
- **Identity:** `(IdP entityID, NameID)`, with persistent NameID preferred.

**Linking a federated identity to a BOSS user.** Linking is always by `(issuer, subject)`, and happens in one of three ways:
1. **Already linked:** sign the user in, provided their status is active.
2. **Signed-in linking:** a signed-in user (who stepped up) chooses *Link SSO* under My account and completes the IdP flow. The resulting `(iss, sub)` binds to them.
3. **Invitation:** an open invitation for email E binds the first verified identity that asserts E with `email_verified=true`. Invitations are single use and expire after 7 days. This is the **only** email-based binding, and it can only bind to an invitation that has not been claimed, **never to an existing account**. That rules out pre-account takeover.

Otherwise sign-in is refused with "Ask an admin to invite you." The one exception is when JIT is on for that domain and `email_verified=true`. Then BOSS creates the user with **no access roles**; group mappings may grant some.

**Group sync.** When the provider sends a `groups` claim (OIDC) or attribute (SAML), BOSS syncs memberships of mapped groups at each sign-in (`source=oidc`). An unmapped IdP group grants nothing.

**MFA with SSO.** If "trust IdP MFA" is on and `amr` contains `mfa`, `hwk` or `otp` (or SAML `AuthnContextClassRef` is in the configured set), BOSS counts MFA as satisfied. Otherwise Owners and Admins still complete BOSS MFA.

**Enforce SSO.** When this is on, users in the matched domains cannot use a password. **Break-glass:** Owners keep passkey sign-in, so an IdP outage cannot lock the org out.

### 4.9 SCIM 2.0

- **Endpoints:** the existing `/scim/v2/{project_id}/Users|Groups` endpoints move to `/scim/v2/` at org level, and the project path stays as an alias for one release.
- **Credentials:** `BOSS_ENTERPRISE_SCIM_BEARER_TOKENS` are replaced by per-provider `boss_scim_…` keys, held by `sys_scim_<provider>`. Those keys can only use `scim:provision`.
- **Users:** SCIM creates people with `status=pending` until their first SSO sign-in. `active=false` means **disable plus the full cascade** (§7.11).
- **Groups:** SCIM groups land read-only (`source=scim`). SCIM **cannot assign access roles.** Admins bind roles to groups.

### 4.10 Lockout, rate limits, anti-enumeration

Rate-limit counters live in Postgres (`identity_rate_limits`, an unlogged table). They are not in memory, so they survive restarts and work with more than one API process. Identifiers are keyed by `HMAC(server_key, lowercased identifier)`, so typed emails are never stored.

| Surface | Limit |
|---|---|
| Sign-in, per client IP | 20/min and 200/h. Then 429 with `Retry-After`. |
| Sign-in, per account | After 5 failures, a progressive delay (1, 2, 4… s, capped at 30 s). After 10 consecutive failures, a **15-minute soft lock of the password factor from unfamiliar devices**. Devices holding a valid `__Host-boss_device` for that account, and passkeys, still work. An attacker therefore cannot lock out the owner. |
| MFA | 5 per challenge, 20/h per user |
| Password-reset requests | 5/h per account identifier and 20/h per IP |
| API key failures | 50/min per IP, then 429 |
| Token exchange | 60/min per run |
| Setup claim | 10/h per IP |
| Magic link and email code requests | 5/h per address identifier and 20/h per IP, shared by both methods; 5 attempts per code (§4.14) |
| Self sign-up requests | 5/h per address identifier and 20/h per IP |

**Client IP.** BOSS uses the socket peer, unless the peer is in `BOSS_TRUSTED_PROXIES` (CIDRs). In that case it takes the right-most untrusted `X-Forwarded-For` hop. A client-sent XFF is never trusted otherwise. This builds on the recent "real client IPs" fix.

**Anti-enumeration.** Unknown users, wrong passwords and locked accounts all return the same body: `{"error":"invalid_credentials"}`. For an unknown user, BOSS runs a dummy argon2id verification so timing matches.
- A disabled account is revealed **only after the correct password** ("This account is disabled. Ask an admin."). Revealing "banned" before checking the password is a common account oracle.
- Forgot-password always returns 202.
- Sign-up is **off by default**. People join by invitation, JIT, SCIM, or, only when an Owner turns it on, self sign-up (§4.15). Self sign-up answers with the same uniform response as forgot-password.

### 4.11 Bootstrap (first Owner)

**Fresh install**
1. When the API starts with zero people, it creates a **setup token**: 32 bytes, stored hashed, with a 1-hour TTL.
2. It prints the claim URL to the logs. `install.sh` prints it too, and `boss identity setup-link` regenerates it.
3. `/claim` asks for a name, email, password, and **mandatory** MFA enrolment (a passkey is offered first).
4. The claimant becomes the first Owner.
5. The setup endpoint then answers **404 for good**.

There are no default credentials, ever.

**Existing install** (`BOSS_CONSOLE_PASSWORD` set): the claim screen accepts the legacy password as the setup credential. The legacy password is proof that you are the operator (§12). After the claim the legacy password maps to Operator for 30 days, then stops working (D25).

**Network-trusted install** (`BOSS_CONSOLE_AUTH=network`, e.g. an install reached over a private network): the network trust that already makes a visitor the operator is the claim credential. See §4.12.

### 4.12 Trusted-network mode is a migration bridge only (today's `BOSS_CONSOLE_AUTH=network`)

Operator decision D27: network-trusted installs move to **passkeys and real accounts**. There is **no Tailscale-header trust** and **no permanent network operator**. Network-trusted mode survives only as the bridge that gets an install from "anyone on the private network is the operator" to "a named Owner signed in with a passkey".

**Before the first claim.** Requests from a private peer (the existing network check) are bridged to `usr_network_operator`, a `network_bound` person:
- It holds an Owner-equivalent set, so behaviour is identical to today and nothing breaks at upgrade.
- It may **claim the first Owner** (below), and it may not touch any other identity setting.
- Every response carries the banner *"Anyone who can reach this BOSS acts as the operator. Claim it to finish setup."*; every audit row records the client IP; writes still need same-origin and CSRF checks; posture is red.

**How a network-trusted install gets its first Owner without setting env or reading logs.** Some installs are operated only through a deploy API; nobody sets environment variables or reads container logs there. So the claim needs neither a setup token nor `BOSS_PUBLIC_URL`:
1. The bridged console shows **Claim this BOSS** to the network-bridged visitor. Network trust is the proof of being the operator, exactly as the legacy password is on a password install.
2. The claim screen shows the **origin it will bind to**, taken from the browser's request (`Origin`, which must equal `Host` and be `https://…`, e.g. `https://boss.example.com`). The claimant confirms it. It is stored as `identity_settings.policy.public_origin` and from then on is the WebAuthn RP ID and the CSRF origin; `BOSS_PUBLIC_URL`, when set, wins. Changing it later needs an Owner with step-up.
3. The claimant enters name and email and **registers a passkey** (required on this path; a password is optional). If the browser has no platform authenticator, a password plus TOTP is accepted instead and posture shows "No passkey on the only Owner".
4. The claim is single-use and rate-limited (10/h per IP); once any real Owner exists it answers 404 for good (A14).
5. `boss identity setup-link` and the logged setup URL (§4.11) still work where an operator can reach them; a network-trusted install does not need them.

**After the claim.** The network bridge maps to **Operator** (D25's role) for at most **30 days** (the same clock as the legacy password), then refuses: visitors see the sign-in page. An Owner can end it sooner with **Require sign-in** (one click, step-up). The network principal is then disabled and its history keeps its name. There is no setting that makes it permanent.

**Not offered:** identity from proxy headers (`Tailscale-User-Login` and the like). A header is an assertion the API cannot verify on its own; passkeys give per-person identity on the private network without it.

### 4.13 The SSRF-safe outbound client

Every fetch made because of identity configuration goes through `boss_engine.identity.http.safe_client`. That covers OIDC discovery, JWKS and token calls, SAML metadata URLs, GitHub, and later CIMD documents. The client:
- resolves DNS once and connects to the pinned IP, which defeats DNS rebinding;
- refuses loopback, RFC 1918, link-local, CGNAT, `169.254.169.254`, ULA and `::1`, **unless the host is in `BOSS_OUTBOUND_ALLOW_PRIVATE`** (a per-host exception, audited, for LAN IdPs such as Keycloak on 10.x);
- requires https, except for explicitly allowed private hosts;
- does not follow redirects to another host;
- uses a 5 s connect timeout, a 10 s total timeout and a 1 MiB body cap.

### 4.14 Email sign-in: magic link and email code (D26; SMTP only)

Both methods exist **only when SMTP is configured**; otherwise their endpoints answer 404 and the console hides the tabs (A17). They are a convenience for people without a passkey, never a second factor and never a recovery bypass.

| Rule | Magic link | Email code |
|---|---|---|
| Request | `POST /v1/auth/magic-link {email}` | `POST /v1/auth/email-code {email}` |
| Response | **Always** `202 {"sent": true}`, identical bytes and similar timing whether or not the address has an account (dummy work for unknown addresses). The link or code is **only ever emailed**; no response, log line or redirect ever carries it (A81, A85). | same |
| Secret | 32 random bytes, base64url | 6 digits from a CSPRNG |
| At rest | `identity_auth_challenges(kind=magic_link)`, `id_hash = SHA-256(token)` | `kind=email_code`, argon2id (small profile) of the code |
| Lifetime | **10 minutes, single use** (A82) | 10 minutes, single use, **5 attempts** then burned (A86) |
| Bound to | the requesting browser: the request sets `__Host-boss_preauth`; `binding_hash` = its hash. A link opened elsewhere does not sign in (A83) | same: the code is accepted only with the same pre-auth cookie |
| Use | The emailed link is `${public origin}/signin/link#t=<token>` (fragment, so it never reaches an access log); the page POSTs it to `/v1/auth/magic-link/verify` (A88) | `POST /v1/auth/email-code/verify {code}` |
| Rate limits | per address identifier (HMAC) 5/h, per IP 20/h, shared across both methods (A86) | same |
| MFA | For roles with MFA required (Owner, Admin, Auditor) the result is a **pre-auth** step: the person must still complete TOTP or a passkey before a session is issued (A87). For others, a session with `auth_methods = {magic_link}` or `{email_code}`; it is **not** fresh MFA and cannot step up. | same |
| Audit | `auth.magic_link.requested/used/rejected` | `auth.email_code.requested/succeeded/failed` |

Email links always use the bound public origin (`BOSS_PUBLIC_URL`, or `policy.public_origin` from §4.12), never the request's `Host` (A11). The email never repeats the typed address back in the page ("sent to alice@…" is itself an oracle).

### 4.15 Social sign-in and self sign-up (D31, D32)

**Social providers** are OIDC presets with fixed issuer and discovery: Google, Microsoft and Apple. GitHub is an OAuth 2.0 preset (no id_token): its subject is the numeric GitHub user id read from `GET /user` with the access token, through the SSRF-safe client, with the issuer recorded as `https://github.com`. Enterprise OIDC and SAML providers appear on the same row by name. Every one follows §4.8: link by `(issuer, subject)` **only**, never by email, whatever `email_verified` says (A89). A first sign-in with an unknown subject creates nothing (A90) unless one of these applies, checked in order:
1. an open, unclaimed invitation for the provider's **verified** email (single use, binds that subject);
2. a JIT domain rule on that provider for the verified email's domain (user created with no access roles);
3. self sign-up is enabled and the verified email's domain is on its allow-list (or the list is empty).

**Self sign-up** is **off by default** (policy `signup.enabled = false`, `signup.domains = []`). When an Owner turns it on:
- the sign-in footer shows **"New here? Create account"**; otherwise **"Ask an admin to invite you"**;
- `POST /v1/auth/signup {name, email}` always answers 202 with the uniform response; a verification link (as §4.14, `kind=self_signup_verify`) is emailed, and only following it creates the person — so SMTP is required, and without it sign-up cannot be enabled;
- a self-signed-up person has **no access roles** until an admin binds one (or a group mapping grants one); `identity_users.self_signed_up` marks them, and posture counts them;
- addresses outside the allow-list get the same uniform response and nothing is created (A91).

---

## 5. Credentials: API keys and service accounts

### 5.1 Format

```
boss_pat_<43 base62 chars: 32 random bytes><6 base62 chars: CRC32 of the body>   personal access token (a person)
boss_sak_…                                                                          service-account key
boss_scim_…                                                                         SCIM provisioning key
boss_egr_…                                                                          per-run egress credential (§7.10), never persisted beyond the run
```

- **Prefix and checksum:** the prefix tells a secret scanner what it found. The checksum lets BOSS reject a typo before any database lookup, and lets scanners match without false positives.
- **Storage:** `api_credentials.token_hash = SHA-256(token)`, with a unique index, looked up by hash. 256 bits of entropy makes a slow hash unnecessary. Comparison is constant-time.
- **Display:** after the shown-once dialog, BOSS shows **no part of the key**. Keys are identified by a random public id (`key_7f3k2q`), a name, the owner, scopes, created, last used (time, IP, client) and expiry. This goes further than products that show a visible prefix, by BOSS rule.

### 5.2 Scopes

- **Permissions:** a key holds a **list of catalogue permissions**. The key's effective set is `owner's live permissions ∩ key.scopes`, evaluated per request. Removing a person's role shrinks their keys immediately.
- **Projects:** a key can optionally be limited to some projects.
- **Network:** a key can optionally carry a CIDR allowlist.
- **Creation:** the scopes must be a subset of what the creator holds (§6.11).
- **Presets in the UI:**
  - *Read-only*: all the owner's `:read` permissions.
  - *MCP client*: the owner's delegable permissions minus the critical ones.
  - *CI deploy*: `deployment:create`, `deployment:read`, `code_task:read`.
  - *Custom*.

### 5.3 Expiry and rotation

- **Default expiry:** 90 days.
- **Maximum:** 365 days, set by policy.
- **No expiry:** allowed only for **service-account** keys, only when an Owner enables it in the policy, and such keys are flagged in posture.
- **Rotation:** `POST …/keys/{id}/rotate` mints a new key with the same scopes. The old key gets a `grace_until` of 0, 1 h, 24 h or 7 d, chosen by the caller, and is revoked automatically when the grace ends. Both keys are visible during the overlap.
- **Expiry notices:** in the console 14 and 3 days before expiry, and by email when SMTP is configured.
- **Unused keys:** keys unused for 30 days show as *stale* in posture. A policy option auto-revokes keys unused for 90 days.
- **Last use:** BOSS records last use (time, IP, user agent) at most once a minute per key.

### 5.4 Service accounts

- **What they are:** non-human principals for CI, scripts and integrations. They never sign in.
- **Owners:** each one has ≥1 human owner. If every owner is disabled, the service account becomes *orphaned*: a posture item and a notice to admins. It is not disabled automatically, because that would break production integrations.
- **Access:** service accounts hold access roles through role bindings, like people.
- **Grant ceiling for keys:** creating a key for a service account requires that the **creator holds every permission the service account holds** (§6.11). Without this, `service_account_key:manage` would be a back door to Admin.
- **Machine-to-machine (later):** RFC 8693 / RFC 7523 exchange of a GitHub Actions OIDC token for a short-lived BOSS token, so CI needs no stored secret (Phase 5).

### 5.5 MCP and CLI clients

- **Now:** external MCP clients (Claude Code, Cursor) and the `boss` CLI authenticate with a **personal access token**. Every action is attributed to that person. The MCP server already strips `project_id` from tool arguments, and that stays.
- **Phase 5:** BOSS becomes an OAuth 2.1 authorization server for its own MCP endpoint. That means RFC 9728 and 8414 metadata, auth code with PKCE and `resource` (RFC 8707), the device flow for headless clients, and Client ID Metadata Documents. Tokens have `aud=boss:mcp`.
- **No passthrough:** BOSS **never** forwards an inbound MCP token upstream.

---

## 6. Authorization

### 6.1 Model

```
permission  = "resource:action"              (catalogue entry, ~180, see IDENTITY_PERMISSIONS.md)
access role = named set of permissions        (built-in: owner, admin, operator, viewer, auditor; plus custom)
binding     = (subject, access role, scope)   subject ∈ {person, group, service account, agent}
                                              scope ∈ {org, project:<id>}
effective(principal, scope) = ⋃ permissions of roles bound to the principal or its groups,
                              at org scope or at that project
```

The model is a union of bindings. It has no deny rules, no wildcards and no inheritance. Agents and keys narrow it further (§6.6).

### 6.2 The catalogue: one file, generated everywhere

The source of truth is `packages/identity/catalogue.yaml`:

```yaml
version: 1
permissions:
  - key: workflow:publish
    title: Publish workflows
    description: Publish a workflow version so it can run.
    category: Workflows and work
    roles: [owner, admin, operator]     # built-in role membership — the ONLY place roles are defined
    scope: project                      # project | org
    step_up: true
    delegable: false                    # may appear in a Passport ceiling / grant
    critical: false
    readonly_post: false
  - key: deployment:production
    …
```

Generated from that file and committed. CI fails if a regenerated file differs from the committed one:
- `engine/boss_engine/identity/permissions_gen.py`: a `Permission` `StrEnum`, plus the metadata dict.
- `apps/web/lib/identity/permissions.gen.ts`: `export type Permission = "workflow:publish" | …`, a literal union with **no `${string}` escape hatch**. A union that accepts any string checks nothing.
- `db/identity_seed.sql`: `identity_permissions` and the built-in `identity_access_roles` / `identity_access_role_permissions`, upserted at boot. A permission removed from the catalogue is removed from custom roles at boot, and that removal is audited.
- The role table in `docs/product/IDENTITY_PERMISSIONS.md`.

**Rules**
- Permissions are **never** created at runtime and **never** auto-created by a role save. An unknown key is rejected with 422.
- No wildcards. "Select all" in the editor expands to explicit keys.
- Adding a permission is a code change. The PR shows the diff to the matrix fixture (§15.5).

### 6.3 Declaring a permission on a FastAPI route

Every route uses `GuardedRoute` as its `route_class`. This applies to the app and to all 49 routers. The declaration is a decorator on the endpoint:

```python
from boss_engine.identity import permit, public, authenticated, Scope

@app.post("/v1/workflow-runs/{run_id}/pause")
@permit(P.WORKFLOW_RUN_CONTROL, scope=Scope.of("workflow_run", path="run_id"))
def pause_run(run_id: str, ctx: Principal = Depends(current_principal)) -> RunCard: ...

@app.get("/v1/code-tasks/{run_id}")
@permit(P.CODE_TASK_READ, scope=Scope.of("code_task", path="run_id"))
def get_code_task(...): ...

@app.post("/v1/delivery-workflows/start")
@permit(P.WORKFLOW_RUN_START, scope=Scope.project(body="project_id"))
def start(...): ...

@app.post("/v1/webhooks/github")
@public(reason="hmac_signed_webhook", verifier=verify_github_signature)
def github_webhook(...): ...
```

- **`GuardedRoute`** reads the declaration when the app is built. **If any route has no declaration, the app refuses to start**, and names the route. A route that somehow reaches runtime without a declaration answers 403. Deny by default happens at boot, not only in CI.
- **`@public(...)`** requires a `reason` from a closed enum and a verifier function. Allowed reasons: `liveness`, `auth_endpoint`, `hmac_signed_webhook`, `oauth_state_bound_callback`, `saml_signed_assertion`, `per_investigation_callback_token`, `jwks`. The full list of public routes is checked into `tests/identity/public_routes.yaml`, and the coverage test compares against it.
- **`@authenticated(...)`** means any active principal, and is used for `/me/*`. Composite views use it too, and must then filter each section by its own permission.
- **Scope resolvers:**

  | Resolver | How it finds the project | Notes |
  |---|---|---|
  | `Scope.org()` | none | org-level permission |
  | `Scope.project(path=…)` | path parameter | |
  | `Scope.project(body=…)` | body field | |
  | `Scope.project(query=…)` | query parameter | |
  | `Scope.of(kind, path=…)` | loads the object by id and takes its `project_id` | object-level check |

  **When a request names an object, the scope comes from the object, never from the request.** If a body names both a `project_id` and object ids, the resolver checks that every object belongs to that project. Otherwise it answers 404.
- **Object visibility:** if the object does not exist, **or** the principal cannot `…:read` in its project, the answer is **404** (no existence oracle). If the principal can read but lacks the action, the answer is 403 with `{"error":"forbidden","permission":"workflow_run:control"}`.
- **Who the actor is:** a handler gets its principal only from `Depends(current_principal)`. The `actor` body field is removed from the schemas. During migration it is accepted and ignored, with a deprecation event (§12).

### 6.4 Declaring a permission on an MCP tool

- **Hand-written tools:** `ToolSpec` gains a required field, `permission: Permission`, with no default, so constructing a spec without one fails when the module is imported. `@reg.tool(...)` passes it through.
- **API-surface tools:** the tools `register_api_tools` generates from OpenAPI **inherit the permission of the route they call**. The route is checked again when the call is dispatched, so the declaration cannot drift.
- **Dispatch:** `_tools_call` calls `authorize(ctx, spec.permission, scope)` before the handler runs. If the caller is an agent run, that check uses the run token's effective set, so non-delegable permissions are always denied.
- **Reversibility:** the rule stays as it is. `destroys_work`, `irreversible` and `grants_authority` are never self-executed. That is a second, independent gate.
- **Coverage test:** it enumerates the registry and fails on a spec with no permission or with a permission that is not in the catalogue. The full mapping of 110 tools is in IDENTITY_PERMISSIONS.md §7.

### 6.5 Declaring a permission on a console page or action

- **Pages:** each `AreaDef` in `ConsoleShell.tsx` gains `permission: Permission`, and the TypeScript type makes it mandatory. Identity sub-pages and My-account pages have their own entries.
  - The **rail** shows only the areas the viewer can open.
  - A **direct URL** to an area the viewer cannot open renders `<AccessDenied permission=… />`, with the reason ("Needs workflow:read") and who can grant it ("Admins: Priya, Sam"). It is never a blank page.
- **Actions:** `<Can permission="workflow:publish">{(allowed, why) => <Button disabled={!allowed} title={why}>Publish</Button>}</Can>`.
  - **Disable with a tooltip; do not hide.** Hiding makes features undiscoverable, and the console UX rule is short labels plus tooltips.
  - Destructive actions the viewer can never take may be hidden entirely, for example "Delete org" for a Viewer.
- **Server actions** (`*-actions.ts`) are wrapped: `export const publish = guardedAction("workflow:publish", async (...) => …)`.
- **Where the client gets its answers:** `GET /v1/identity/me/permissions?project_id=` returns `{epoch, permissions[], step_up_fresh_until}`. The client refetches when the epoch changes, which the existing console socket pushes. The UI never decides anything; the API is checked again on every call.
- **Tests:**
  - Every `AREAS` entry has a permission, and every permission literal used in `apps/web` exists in the catalogue. Both are type-checked, and a test asserts them too.
  - Every server action is wrapped.
  - A Playwright walk as **Viewer** clicks every enabled control and asserts that no mutating request returns 2xx.

### 6.6 The enforcement point (PDP)

There is one function, in `engine/boss_engine/identity/pdp.py`, used by routes, MCP tools, server-rendered console data, the token service and the egress proxy.

```python
def authorize(ctx: AuthContext, permission: Permission, scope: Scope) -> Decision:
    # 0. catalogue
    meta = CATALOGUE.get(permission)                    # unknown → deny (and 500 in dev/test)
    # 1. liveness of everything in the context (cached ≤5 s, invalidated by NOTIFY)
    require_active(ctx.principal)                       # status, org match
    if ctx.session: require_session_live(ctx.session)
    if ctx.credential: require_key_live(ctx.credential)  # not revoked/expired, CIDR, project limits
    if ctx.run_token:
        require_token_live(ctx.run_token)               # jti not revoked
        require_grant_live(ctx.run_token.grant)         # active, not expired, user active
        for agent in ctx.run_token.chain: require_passport_active(agent)
    # 2. system principals: fixed sets
    if ctx.principal.kind == "system":
        return allow_if(permission in SYSTEM_SETS[ctx.principal.id])
    # 3. base set
    if ctx.run_token and ctx.run_token.mode == "obo":
        base = (perms(ctx.run_token.sub, scope)                       # the human, LIVE
                & ⋂(ceiling(a) for a in ctx.run_token.chain)          # every agent in the chain
                & grant_perms(ctx.run_token.grant)
                & ctx.run_token.scope                                  # the minted narrowing
                & task_scope_allows(ctx.run_token.task, scope))       # project/resource limits
        if not meta.delegable: return deny("not_delegable")
    elif ctx.run_token and ctx.run_token.mode == "autonomous":
        base = perms(ctx.run_token.agent, scope) & ceiling(ctx.run_token.agent) & ctx.run_token.scope
        if not meta.delegable: return deny("not_delegable")
    else:
        base = perms(ctx.principal, scope)
        if ctx.credential: base &= ctx.credential.scopes
    # 4. step-up
    if meta.step_up and not step_up_fresh(ctx): return deny("step_up_required", status=401)
    # 5. decision
    return allow(via=explain(...)) if permission in base else deny("missing_permission")
```

- **`perms(principal, scope)`** is the union of the role permissions bound directly or through groups, at org scope or at `project:<id>`. It is computed from SQL and cached per `(principal, scope, identity_epoch)`.
- **`identity_epoch`** is one monotonically increasing counter per org. Every identity mutation bumps it in the same transaction and sends `NOTIFY identity_epoch`. That covers bindings, roles, group membership, status, grants, Passports, keys and sessions. Every API process and the egress proxy LISTEN and drop their caches. The cache TTL of 5 s caps staleness if a notification is lost. **Revocation latency is ≤5 s, and in practice it is milliseconds.**
- **`explain()`** returns the *why* chain, for example `allow via role operator ← group Builders ← binding at project:acme-web`. The console's "Effective access" view uses it, and every audited decision stores it.
- **Denials are always audited** as `authz.denied`, with the full context. **Allows are audited** for critical permissions. Other allows are counted, not logged row by row.
- **Shadow mode.** `BOSS_AUTHZ_MODE=shadow|enforce`, set globally and overridable per router. In shadow mode `authorize()` runs and records `authz.shadow_deny`, but the old guards decide. The migration flips routers to enforce as each one's shadow denials reach zero (§12). The identity API itself is always enforced.

### 6.7 Scoping

- **Org binding:** applies to every project, including future ones.
- **Project binding:** applies to that project only. Permissions flagged `scope: org`, which covers identity, the org and security policy, cannot be bound at project scope. The binding API rejects them.
- **Guests:** a person with only project bindings sees only those projects. The project switcher and every list endpoint filter by `project:read`.
- **Lists:** list endpoints never take an unchecked `project_id`. They return only the projects where the caller holds the resource's `:read`, enforced in the query.
- **Org boundary:** the org always comes from the principal. A row from another org is invisible (404). Every identity query is scoped by `org_id`, and a cross-org test runs on every route (§15.3).

### 6.8 Groups

- Groups are flat, with no nesting.
- Their sources are manual, SCIM and OIDC. The last two are read-only in BOSS.
- Role bindings can target a group. Members inherit those roles at the binding's scope.
- **Changing a group's membership is granting.** Adding someone to a group requires `group:manage_members` **and** that the caller holds every permission the group's roles carry at those scopes (§6.11).
- The person profile shows group-derived access with its source: "via **Builders** (from Okta)".

### 6.9 Built-in roles

There are five built-in access roles: **Owner, Admin, Operator, Viewer, Auditor**. There are three fixed system roles: **system_worker, system_collector, system_scim**. IDENTITY_PERMISSIONS.md §2–3 has the full permission table.

| | Owner | Admin | Operator | Viewer | Auditor |
|---|---|---|---|---|---|
| Permissions held (of 180) | 175 | 166 | 83 | 46 | 59 |
| Bindable at | org | org, project | org, project | org, project | org |
| MFA required | yes | yes | policy (default no) | policy (default no) | yes |
| Typical holder | founder, platform owner | team lead, platform engineer | engineers and PMs who run the factory | stakeholders | compliance |

Built-in roles are **read-only**. To customise one, **clone** it.

### 6.10 Custom roles

Custom roles are created from scratch or cloned. A custom role is a name, a description, a list of permissions, and the scopes it can be bound at.

**Editor rules**
- The permissions you can add are limited to what you hold. Permissions you lack are shown locked, with "you don't hold this".
- Critical permissions need a typed confirmation.
- Every save shows the **diff and impact preview** before it applies:
  - who is affected, directly and through groups;
  - which pages they gain or lose;
  - how many API operations and MCP tools they gain or lose;
  - which agents' effective access changes, through grants that include those permissions.

**Versioning:** every save writes a role version, `identity_access_role_versions`. The editor shows that history and can revert to an earlier version. A revert is itself a new save, subject to the same checks.

**Deletion:** a role cannot be deleted while bindings exist. The editor offers "Reassign 4 people to…" and then deletes.

### 6.11 Grant limits (no escalation)

All of these checks run in the API, in the same transaction as the write.

| Action | Requirement beyond the permission itself |
|---|---|
| Bind role R at scope S to subject X | The caller holds ⊇ R.permissions at S. **X ≠ the caller** (nobody edits their own bindings). R = owner requires `owner:assign`. |
| Remove a binding of R | The caller holds ⊇ R.permissions at S. So an Admin cannot remove an Owner. |
| Create or edit custom role R | The caller holds ⊇ (old R ∪ new R) at every scope R is bound. |
| Add a member to group G | The caller holds ⊇ the permissions of G's bindings. |
| Invite with roles | The invited roles ⊆ the inviter's. |
| Create an API key | key.scopes ⊆ the owner's permissions, and the creator ⊇ the owner's permissions when the owner is someone else (a service account). |
| Change a Passport ceiling | The caller ⊇ the new ceiling. |
| Create a delegation grant | grant ⊆ the user's permissions ∩ the agent's ceiling, at grant time. It stays live-intersected afterwards. |
| Any change by an agent | Agents can never hold `role_binding:assign`, `access_role:manage`, `delegation:grant` or `agent_passport:manage`. None of them are delegable. |

**Mass assignment.** Every PATCH schema is an explicit allowlist. Fields such as `role`, `org_id`, `status`, `is_owner` and `kind` are rejected when they are not allowed. They are not silently ignored.

### 6.12 Last-Owner protection

- **API:** owner changes take a per-org advisory lock and check that ≥1 *active, non-network-bound* Owner remains.
- **Database:** a `DEFERRABLE INITIALLY DEFERRED` constraint trigger on `identity_role_bindings` and `identity_principals` raises at commit if an org would have zero active Owners. Two concurrent demotions cannot both commit.
- **Disabling or deleting the last Owner** is refused, with "Add another Owner first."
- **Break-glass:** `boss identity recover-owner` (§4.7).

### 6.13 Approvals and seats (separation of duties)

To approve a decision, all four of these must hold:
1. **`decision:approve`.** It is not delegable, so agents can never approve, including their own step-up requests.
2. **The seat the risk tier requires**, from `approval_seats`, mapped from today's `ceo` and `cao` role keys.
3. **A human session.** API keys and run tokens cannot approve. MCP confirmations keep their human-approval flow.
4. **For tier 3, two distinct people.** The exception is when the org policy `single_operator_waiver` is on. It defaults to on while the org has exactly one active person, and posture shows it (kept by operator decision D29).

### 6.14 Naming

- **Access role** (`access_role` in code, "Access roles" in the console) means an RBAC role.
- **Role** keeps its BOSS meaning: an agent's job, as used in Agent Studio, `roles`, `role_skills` and `role_key`.
- Code and copy never write "role" alone in identity contexts.
- The permission for agent definitions is `agent:*`, not `role:*`.
- The new tables are prefixed `identity_`, so nothing collides with the existing `roles` table.

---

## 7. Agents: Passports, delegation, tokens, vault

### 7.1 The Passport (upgrade of today's agent identity)

The existing `agent_identity.py` columns on `roles` keep their meaning: `agent_id` (immutable), `born_by`, `born_from`, `sponsor`, `lifecycle` and `fingerprint`. The Passport gains `identity_agent_passports`, keyed by `agent_id`, which is also an `identity_principals` row of `kind=agent`.

| Field | Meaning |
|---|---|
| `sponsor_principal_id` | The **required** accountable human. If the sponsor is disabled, the agent is flagged for review. |
| `ceiling` | The most anyone may delegate to this agent. It is a set of **delegable** catalogue permissions (`identity_agent_ceilings`). Presets are in IDENTITY_PERMISSIONS.md §4. |
| `external_ceiling` | Per provider, for example `github: {contents: write, pull_requests: write, issues: read}`. |
| `can_spawn` | Whether the agent can start sub-agents, plus `allowed_children` (a list of agent role keys). |
| `max_depth` | ≤ the org maximum. Default 3, hard cap 5. |
| `autonomous` | Whether the agent may run as itself (§7.2), and with which access-role bindings. |
| `status` | `active`, `suspended` or `revoked`. Revoked is terminal. Re-issuing creates a new Passport version, with lineage `reissued_from`. |

`seal_identity` keeps emitting `agent.sealed`. A sealed Passport's ceiling can only change through `agent_passport:manage`, and the change goes into lineage with a before/after diff.

### 7.2 Two modes: on behalf of a person (default) and as itself

| | **On behalf of** (default) | **As itself** (autonomous) |
|---|---|---|
| When | A person started the work: from the console, the BOSS Agent chat, an MCP client using their PAT, or a schedule they own | BOSS started it: shepherd sweeps, on-call auto-remediation, focused-agent ticks, system-doctor findings |
| Token `sub` | the person (`usr_…`) | the agent (`agt_…`) |
| Token `act` | the agent instance, nested for sub-agents | none (it is the agent) |
| Authority | person ∩ ceiling(every agent in the chain) ∩ grant ∩ task | agent's own bindings ∩ ceiling ∩ task |
| UI and audit say | "**Forge** for **Alice**" | "**Beacon** on its own" |
| External identity (GitHub) | BOSS App token, with Alice recorded in lineage and in the commit trailer, or Alice's own OAuth when the grant says "as me on GitHub" | BOSS App token, with the agent recorded |

**On "impersonate the logged-in user".** By default an agent acts **with the person's authority**: what it may do is exactly what that person may do, narrowed. The record never pretends the person did it. Every token carries `act` (RFC 8693 delegation, not impersonation), so the audit always says "Forge, for Alice". Erasing the agent from the record would make prompt-injected actions indistinguishable from the human's own.

### 7.3 Delegation grants

A grant is the long-lived, revocable answer to "may this agent act for me, and how far?" It is a row in `identity_delegation_grants`:

```
dlg_01JB…   user: Alice   agents: [Forge, Vera] | preset "Builders"
            permissions: code_task:create, code_task:steer, github:sync, preview:manage, …   (⊆ Alice ∩ ceilings)
            projects: [acme-web]           resources: {github: {repos: [acme/web], contents: write, pull_requests: write}}
            sub-delegation: allowed → [Vera, Rook]     max depth: 3
            expires: 2026-11-05 (30 d)     purpose: "Build and review acme-web"
            status: active                 last used: 3 min ago by run_8812
```

**How grants get created**
1. **Standing grant.** Under My account → *Agents acting for me* → *Let an agent act for me*, the user picks:
   - the agents or a preset;
   - permissions from their own ∩ the ceiling, shown as plain-English "abilities" that map to permissions;
   - projects and repos;
   - an expiry: 7, 30 or 90 days, or "until revoked" only if the policy allows it.
2. **Just-in-time consent.** When a person starts work whose plan needs more than their standing grants cover, the console shows a consent sheet, for example: *"Forge needs to push to `boss/*` on acme/web and open a PR. [Allow for this build] [Allow for 30 days] [Cancel]."* "For this build" creates a **task-bound grant** that ends with the run. The sheet names the specific agents, which follows the OBO-for-agents draft's UX.
3. **Schedules.** A schedule needs a grant from its creator that outlives it. When the grant expires, the schedule pauses with *"Renew authority"*. It never silently runs with stale authority.

**Rules**
- A grant can never exceed the user's live permissions intersected with each named agent's ceiling. That is checked at creation and re-intersected live at every use, so demoting Alice demotes her agents immediately.
- A grant names its agents (the `may_act` analogue). Sub-delegation needs `allow_subdelegation` and an allow-list of child agents.
- `delegation:grant` is not delegable. An agent cannot create or widen a grant.
- **Defaults:**
  - Expiry defaults to 30 days and the standard maximum is 90 days.
  - BOSS reminds the user 3 days before expiry.
  - Grants unused for 14 days show as *idle* in posture.

### 7.4 Effective permission at hop n

```
effective(hop_n) = perms_live(user, scope)
                 ∩ ceiling(agent_1) ∩ … ∩ ceiling(agent_n)
                 ∩ grant.permissions
                 ∩ token.scope            (the narrowing requested at each exchange; ⊆ the parent token's)
                 ∩ task(run): projects, repos, the workflow step's declared needs
  and permission.delegable = true
  and depth n ≤ min(org.max_depth, grant.max_depth, every passport's max_depth along the chain)
  and no agent appears twice in the chain
```

**Depth cap: the minimum along the chain.** A child can never raise it.

### 7.5 The token service (RFC 8693)

**Token families.** Each family has its own signing key, `typ` and audience:

| Family | Format | `typ` | Lifetime | Audience | Key |
|---|---|---|---|---|---|
| Console session | opaque `bss_` | none | 12 h idle / 7 d | none | none (hash in DB) |
| API key | opaque `boss_pat_` / `boss_sak_` | none | ≤365 d | none | none (hash in DB) |
| **Run token** | JWT, EdDSA (Ed25519) | `boss-run+jwt` | **5 min**, renewed by the supervisor | exactly one of `boss:api`, `boss:mcp`, `boss:egress` | `run` key family |
| **Actor assertion** | JWT, EdDSA | `boss-actor+jwt` | 60 s | `boss:token` | `actor` key family |

Signing keys live in `identity_signing_keys`, with the private key encrypted under the vault KEK. They rotate every 30 days. **Two keys are published at a time**, at `/.well-known/boss-jwks.json`. BOSS refuses to boot without its keys and never generates an ephemeral key silently. A silent ephemeral key is a common flaw.

**The exchange.** `POST /v1/identity/token`, with `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`:

| Parameter | Value |
|---|---|
| `subject_token` | the parent run token (with a DPoP proof), or, for the first hop, an internal call from the run supervisor carrying the person's authenticated request context |
| `actor_token` | the child's actor assertion, which the run supervisor mints when it creates the child run for that Passport |
| `audience` | one of `boss:api`, `boss:mcp`, `boss:egress` |
| `scope` | the requested narrowing |
| `requested_token_type` | `urn:boss:token-type:run` |

**Server checks, in order. Any failure denies:**
1. Verify `subject_token`:
   - signature with a local JWKS (`kid` resolved only locally, **never** `jku`, `x5u` or `jwk`);
   - `alg` must be EdDSA, and `none` or HS* are rejected;
   - `typ`, `iss`, `aud=boss:token` or `boss:api`, `exp`, `nbf`, `iat` with 30 s of skew;
   - the DPoP proof;
   - the jti is not revoked.
2. Verify the actor assertion in the same way. Its Passport must be active, and its run must be a child of the subject token's run.
3. **The caller may delegate to this child:**
   - the parent's Passport has `can_spawn`;
   - the child ∈ `allowed_children`;
   - the grant has `allow_subdelegation`, and the child ∈ the grant's child allow-list;
   - same org.
4. Depth: `depth+1 ≤ min(org.max_depth, grant.max_depth, every passport's max_depth)`, and the child is not already in the chain.
5. Re-check, live, that every principal in the chain is active and that the grant is active.
6. `new_scope = requested ∩ parent.scope ∩ ceiling(child)`. **Empty means empty, never "the requested set".** Falling back to the requested set is a common flaw.
7. `exp = min(now+300 s, parent exp + 0, grant.expires_at)`.
8. Mint the token, write the `token.issued` lineage row, and return it.

**Run token claims.** Example: Alice → Atlas (plans) → Forge (builds):

```json
{
  "iss": "https://boss.example.com", "typ": "boss-run+jwt", "aud": "boss:api",
  "sub": "usr_01J9ALICE", "boss_org": "org_boss", "boss_mode": "obo",
  "act": { "sub": "agt_forge#run_8812", "act": { "sub": "agt_atlas#run_8811" } },
  "boss_chain": ["agt_atlas#run_8811", "agt_forge#run_8812"], "boss_depth": 2,
  "boss_grant": "dlg_01JB7Q", "boss_task": { "run": "run_8812", "project": "prj_acme_web",
                 "step": "build", "resources": { "github": ["acme/web"] } },
  "scope": "code_task:read code_task:steer repository:read github:sync preview:manage",
  "cnf": { "jkt": "0ZcOCORZNYy-DWpqq30jZyJGHTN0d2HglBV3uiguA4I" },
  "jti": "rt_01JB7R…", "boss_parent_jti": "rt_01JB7P…", "iat": 1791300000, "exp": 1791300300
}
```

- **`act` nesting** follows RFC 8693: the outermost `act` is the current actor. BOSS goes further than the RFC and **evaluates every actor**, intersecting the ceilings of the whole chain.
- **`boss_chain`** is a flat copy of the same chain, so policy code never has to recurse.
- **No email and no names** in tokens. Payloads are minimal.

**Introspection.** `POST /v1/identity/introspect` requires the `boss:egress` service credential. It is **never public**. It returns `active`, `act`, the chain and the grant status. The Keycloak issue tracker shows introspection that drops `act` (#53029), so a test covers that it does not.

### 7.6 Run tokens inside sandboxes

**The boundary (as built).** A code task's agent, its tools, its verify commands and its review seats run as a **per-run unprivileged uid** from a fixed pool (61000–61999), never as the API's uid (`services/sandbox_identity.py`). The only privileged piece is `boss-sandbox-exec` (`scripts/sandbox/boss-sandbox-exec.c`): root:boss 0750 with `cap_setuid,cap_setgid,cap_chown,cap_fowner`, it only starts a command as a pool uid (new session, no groups, `no_new_privs`, parent-death kill), kills every process of a pool uid, and hands a run its directory and its shim socket. So the agent cannot read the API's `/proc/<pid>/environ`, the identity key volume, another run's directory, `mcp.json` or shim socket. Each run's directory is `<uid>:boss 2770`; its shim socket is `<uid>` 0600 in a boss-owned 0711 directory and the shim answers only the peer uid the kernel reports (`SO_PEERCRED`). The API container's entrypoint (`scripts/sandbox/entrypoint.sh`) starts as root with `NET_ADMIN` only to fence the pool's network with iptables — loopback addresses, and on the container's own networks only the API (8000) and boss-egress (8090, and 8091: an allowlisted HTTPS `CONNECT` proxy that is the model gateway and package route) — then becomes `boss` for good. `BOSS_SANDBOX_ISOLATION=required` (compose's default) refuses a build when the helper or the fence is missing; `auto` (a developer's Mac) runs unisolated and logs it loudly. A51 is proven in a Linux container with the real helper and fence (`engine/tests/test_identity_f1_sandbox_boundary.py`).

- **Key generation:** when a sandbox (microVM or container) boots, its **credential shim** generates an Ed25519 DPoP key pair. The shim is a small process that runs as a separate uid and is not part of the agent's workspace. The private key never leaves the shim's memory.
- **Handshake:** the run supervisor receives the public key over the sandbox bootstrap channel and binds the run token to it (`cnf.jkt`).
- **Use:** the agent's tools (`boss` CLI, the MCP client, `git`, `gh`) call the shim over a unix socket. The shim attaches the token and a fresh DPoP proof. **The model never sees either the run token or the key**, and the shim renews the token every 4 minutes.
- **Network egress from the sandbox** is limited to the BOSS API, the model gateway and the egress proxy (§7.10). A token that leaks anyway is useless elsewhere: it is audience-bound, DPoP-bound, unreachable from outside the network, and expires within 5 minutes.
- **Harnesses outside BOSS's sandboxes**, such as a coding agent the operator runs on their own laptop through MCP, use the person's PAT. They are attributed to that person as a person, not as an agent. This shows as a distinct lineage kind: "via MCP client".

### 7.7 Sub-agents, depth and cycles

- **Spawning:** a sub-agent is always spawned through BOSS, via `spawn_subagent` or a workflow step. That call creates the child run, the actor assertion and the exchange (§7.5). An agent cannot mint a token for another agent by itself.
- **Depth limits:** the default maximum depth is **3 agents** below the person, and the hard cap is 5. Each Passport and grant can only lower it.
- **Cycles:** a cycle (the same `agt_` appearing twice) is rejected.
- **Panels** (meetings) run their seats as parallel children of the facilitator run. They are siblings at the same depth, not a chain.

### 7.8 The token vault

The `identity_vault_items` table holds every long-lived external secret BOSS owns:
- users' OAuth tokens (GitHub user-to-server, Google, YouTube channel connections);
- the GitHub App private key;
- provider credentials, migrated from `secret_refs` over time;
- OIDC client secrets;
- TOTP secrets, through the same envelope encryption.

| Item | Spec |
|---|---|
| Encryption | Envelope encryption. Each row has a random 256-bit DEK, used with AES-256-GCM and a 96-bit random nonce. **AAD = `item_id ‖ owner_principal_id ‖ provider ‖ kind`**, so a ciphertext moved to another row fails to decrypt. |
| KEK | Read from `BOSS_MASTER_KEY_FILE` (preferred; Docker secret) or `BOSS_MASTER_KEY` (32 bytes, base64). A **key check value** is stored. **Boot refuses** (`vault.boot`) when the key does not match, or when the key file is missing for a vault that has data; the API never serves with a vault it cannot open. The key file is created only at **first install** (no registered KEK, no item), explicitly and logged — a runtime read never creates one. External KMS (AWS KMS, GCP KMS, Vault Transit) is optional, in Phase 5. |
| Rotation | `boss identity rotate-master-key` re-wraps every DEK in one transaction and audits it. |
| Who may decrypt | Only `boss_engine/identity/vault.py`. A CI test fails if any other module imports `vault._decrypt`. |
| Failure | A decryption failure is a hard error plus a `vault.decrypt_failed` critical event. **There is never a plaintext fallback.** A plaintext fallback is a common flaw. |
| Provider refresh | On demand, when the token is requested with less than 5 minutes left. It is **single-flight**: `SELECT … FOR UPDATE` on the row, refresh, then write the new refresh token — all in the vault's **own transaction, committed before the caller continues**, so a rollback of the caller's business transaction can never lose a refresh token the provider already burned. If the provider refuses or rotated but the write failed, the item becomes `needs_reauth` (also in its own transaction, so it sticks) rather than retried in a loop. A background sweeper only touches refresh tokens that are close to their 6-month expiry. |
| Revocation | Disconnecting, or deleting a user, calls the provider's revoke endpoint **first**; the local ciphertext is shredded only once the provider confirms. If the provider is unreachable the item is revoked locally at once (unusable) but kept, `provider_revoke_pending`, and identity maintenance retries. For GitHub that is `DELETE /applications/{client_id}/grant`. |

### 7.9 GitHub strategy

**Default: the GitHub App, with an installation token per run.**
- **Minting:** when the run starts, BOSS mints an installation access token with `repositories=[the run's repo]` and `permissions = grant.github ∩ passport.external_ceiling ∩ step needs`, for example `{contents: write, pull_requests: write, metadata: read}`. GitHub caps the lifetime at 1 h. BOSS **revokes it at run end** (`DELETE /installation/token`).
- **Where it lives:** the token stays in the egress proxy's memory. It is not written to the vault and never enters the sandbox.
- **Attribution:**
  - The PR and commits show **BOSS[bot]**.
  - Each commit carries trailers: `On-behalf-of: Alice <alice@acme.com>` and `Boss-Lineage: dlg_01JB7Q/run_8812`.
  - The PR body ends with a link to the lineage page.

**Confused-deputy guard: the App must not reach further than the person.**
- An org's App installation usually covers many repos. Without a guard, an Operator who cannot write to `acme/infra` on GitHub could get Forge to push there.
- So when a grant names GitHub resources, BOSS checks that the granting person **has at least that access on GitHub**, using their linked GitHub identity (`GET /repos/{o}/{r}` → `permissions`, checked with their user token).
- It checks at grant time and again at each run start, cached for 15 minutes.
- **Policy `github.require_user_repo_access`** (D28): it applies **only on multi-person installs**, meaning more than one active human principal (network-bridge and legacy principals do not count). On a one-person install the person is the only one who could be confused-deputied, so the guard is off and costs nothing. The moment a second person becomes active it switches on, and existing grants naming GitHub resources are re-checked at their next run start.

**The user's own OAuth.** BOSS uses GitHub App user-to-server tokens, vaulted with rotating refresh, **only** when the grant says "act as me on GitHub". Two cases need it:
- the action must count as the human on GitHub, for example an approving review toward branch protection;
- the App is not installed on the repo.

These tokens take the egress path too. The token stays in the vault and proxy.

**Classic and fine-grained PATs.** Today's `personal_access_token` connections are migrated into the vault, marked **legacy**, and allowed only for Admins. Posture shows them in red with a "Switch to the GitHub App" call to action.

**Pushes.** Run tokens may only push to `refs/heads/boss/<run-id>-*`. The proxy inspects receive-pack ref updates. Force-pushes to other refs, deleting the default branch, and writes to `.github/workflows/*` are denied unless the grant explicitly includes `workflows: write`.

### 7.10 Egress credential injection

`boss-egress` is a reverse proxy. It runs next to the API, and as a sidecar on remote worker hosts.

- **Routing:** `git` in the sandbox is configured with `url."https://egress.boss.internal/github/".insteadOf "https://github.com/"`, and `gh` and the GitHub API base URL point at `https://egress.boss.internal/github-api/`.
- **Why a reverse proxy, not a TLS-intercepting proxy:** there is no MITM CA to inject or leak.
- **Sandbox credential:** the sandbox authenticates with its per-run `boss_egr_…` credential, presented by the shim's git credential helper. That credential is random, bound to the run and the sandbox's network identity, valid for ≤1 h, revoked at run end, and stored hashed.
- **Per request, the proxy:**
  1. Maps `boss_egr` to the run, then to the run token, and checks liveness (grant, chain, user).
  2. Checks the request against the **rule table** for the run's effective GitHub permissions: method and path-template allowlists, ref rules for pushes, no admin endpoints (`/collaborators`, `/hooks`, `/keys`, `DELETE /repos`, …).
  3. Injects `Authorization: Bearer <installation token>` and streams the response.
  4. Writes an `egress_log` row: method, path template, status, bytes, run, jti, chain. Bodies and tokens are never recorded.
- **Fallback (JIT token):** this is for harnesses that cannot use the proxy. `POST /v1/identity/credentials/github` returns a down-scoped installation token for ≤1 h, for the run's repo only.
  - It is off by default, enabled per policy.
  - Every issuance is a `credential.handed_out` lineage event.
  - Posture counts these events, because a handed-out token is a token that can leak.

### 7.11 Revocation cascade

The target is effect within 5 seconds, end to end, and a test proves it (§15.3).

| Trigger | Effects, all in one transaction plus NOTIFY |
|---|---|
| **Grant revoked** (by the user, `delegation:revoke_any`, or expiry) | Grant → `revoked`. Every child grant → `revoked`. Run tokens carrying it fail their live check. Their egress credentials are revoked. Installation tokens minted for those runs are revoked at GitHub. Running runs → `halted: authority revoked` (paused; resumable only after the same person grants again). Dependent schedules are paused. |
| **Person disabled** (admin, or SCIM `active=false`) | Sessions are revoked. PATs are suspended, not deleted. Grants are suspended. Runs on their behalf are halted. Their vault items become unusable. Service accounts they solely own are flagged orphaned. |
| **Person deleted** | Everything above, plus provider-side revocation of their OAuth grants, a wipe of their vault items, and erasure of their PII. Audit rows keep a tombstone id. |
| **Passport suspended** | Every run whose chain contains the agent is halted. Grants naming it are paused. Its autonomous schedules are paused. |
| **Passport revoked** | Terminal. Grants naming it are revoked. Passports born by it (`born_by=agent:<id>`) are **suspended pending review**, the whole subtree and not only direct children (flagging only direct children is a common gap). |
| **API key revoked or expired** | Immediate: it is a hash lookup with no cache beyond the epoch. |
| **Session revoked** | Immediate. |
| **Role or binding removed** | The epoch bumps. Live intersection shrinks every OBO token of that person at its next call. No token reissue is needed. |

Revoked is terminal for grants and Passports. "Undo" means creating a new grant, which shows in the timeline.

### 7.12 Lineage

- **Where it lives:** in the security event log (§8) as `stream = lineage`.
- **What it records:**
  - Passport events: `born`, `sealed`, `ceiling_changed` (with a diff), `sponsor_changed`, `suspended`, `revoked`, `reissued`.
  - Grant events: `created`, `used_first`, `narrowed`, `revoked`, `expired`.
  - Tokens: `token.issued` and `token.exchanged`, with jti, parent_jti, sub, chain, grant, run, aud and a scope hash.
- **Reconstruction:** every audited action stores the `jti` it used, so any action can be walked back: **action → jti → parent jti → … → grant → person**, and forward from any grant to everything done under it.
- **Console views:** lineage is shown by run, by grant, by person and by agent (screen 9.6).

### 7.13 What agents can never do

- Approve a decision.
- Assign or edit access roles.
- Create, widen or revoke someone else's grant.
- Change a Passport.
- Read a secret value.
- Use `host:exec`.
- Deploy to production.
- Waive gates.

These follow from the permissions not being delegable. In addition, the token service refuses to mint any scope that contains a non-delegable key. That is two independent checks.

### 7.14 Jobs and workers

- **What a job carries:** every job row (`jobs` and related tables) carries `on_behalf_of` (a principal), `grant_id` and `chain`. A job with no principal **fails closed** at enqueue, and a test enforces it.
- **What the worker holds:** the worker authenticates as `sys_worker`, which holds `worker:lease`, for the protocol endpoints only (poll, heartbeat, complete).
- **How effects are authorized:** when the worker leases a job, the API mints the job's run token, scoped to the job's principal, grant and task. Every effect the job causes uses that token. The worker's own identity never authorizes an effect.
- **Leases:** existing lease tokens stay. Both the lease and the run token must be valid.
- **Jobs with no agent (as built).** A leased job whose work is an agent's (a build, an agent loop, a workflow step, a skill) runs under a run token minted for its principal, grant and task, as above. A job whose work is BOSS delivering something with no agent in it (an outbox message, a webhook, an a2a dispatch, an infrastructure action) has no agent to put in a run token's chain, so it gets none. Instead its effect is authorized **when it is queued**: every job kind maps to the catalogue permission its effect needs (`boss_engine/identity/job_permissions.py`, one table, a test fails on an unmapped kind a worker leases), checked by the PDP at the job's project for the person or run that queues it; an unknown kind is refused under enforcement. The job row still carries `on_behalf_of`, `grant_id` and `chain`, so the cascade cancels it when that authority ends, and the worker's own identity still authorizes nothing.

---

## 8. Audit: the security event log

### 8.1 Table and integrity

`identity_security_events` (DDL in §11) is append-only:
- The table, its trigger function and the two maintenance functions belong to the NOLOGIN role `boss_audit_owner`. The app's DB role has `INSERT, SELECT` and `EXECUTE` on those functions only (`identity_audit_grant_app(role)` grants another app role the same).
- A trigger raises on `UPDATE`, `DELETE` **and `TRUNCATE`** for every session. The only exceptions run inside `identity_audit_prune` (deletes only a range the newest event — an `audit.checkpoint` — names) and `identity_audit_rekey` (hash columns of pre-keyed rows only), as `boss_audit_owner`. No session setting opens the table.
- Rows are **hash-chained per org** with a **keyed** hash: `hash = HMAC-SHA-256(chain key, prev_hash ‖ canonical_json(row minus hash))` (`hash_alg = 2`), with a per-org `seq`. The chain key is 32 random bytes per org held in the vault (envelope-encrypted under the master key, so the database alone cannot recompute a hash, and a master-key rotation re-wraps it without changing it). Rows written before the keyed chain are re-keyed once by maintenance, after their old SHA-256 chain verifies.
- `POST /v1/identity/audit/verify` walks the chain and reports the first break, refuses unkeyed rows in a keyed chain, and checks every published daily anchor is still in the chain (a chain cut back is a break).
- When the app's role is a Postgres **superuser** (a stock compose install), privileges cannot bind it; the triggers and the keyed chain still make tampering detectable.
- A daily anchor (the latest hash and seq) goes into the activity log, and into the export when export is configured.

Writes to this log are **not best-effort**. A security event is written in the same transaction as the change it describes. If the write fails, the change fails. Swallowing audit exceptions is a common flaw.

**Retention:** 400 days by default. Only an Owner can prune (`activity:prune`, step-up), and a prune writes a `checkpoint` event that holds the hash of the pruned range, so the chain stays verifiable.

**There is no write API.** Audit rows are produced only by the code paths below. A public audit write endpoint is a common flaw.

### 8.2 What is logged

| Group | Events |
|---|---|
| Sign-in | `auth.login.succeeded`, `auth.login.failed` (identifier HMAC, reason class), `auth.mfa.succeeded`, `auth.mfa.failed`, `auth.passkey.*`, `auth.locked_soft`, `auth.new_device`, `auth.step_up`, `auth.logout` |
| Sessions | `session.created`, `session.rotated`, `session.revoked` (by whom) |
| Credentials | `password.changed`, `password.reset_requested`, `password.reset_completed`, `mfa.enrolled`, `mfa.removed`, `recovery_codes.generated`, `recovery_code.used`, `passkey.added`, `passkey.removed`, `passkey.counter_regressed` |
| Admin | `user.invited`, `user.updated`, `user.disabled`, `user.enabled`, `user.deleted`, `user.mfa_reset`, `group.*`, `group.member_added`/`removed`, `access_role.created`/`updated` (diff)/`deleted`, `role_binding.added`/`removed`, `owner.added`/`removed` |
| Keys | `api_key.created`/`rotated`/`revoked`/`expired`, `api_key.first_used`, `api_key.used_from_new_ip` |
| SSO and SCIM | `sso.login`, `sso.link`, `sso.unlink`, `sso.provider.created`/`updated`, `scim.user.*`, `scim.group.*` |
| Agents | `agent.passport.*`, `delegation.*`, `token.issued`, `token.exchanged`, `token.denied`, `credential.handed_out` |
| Vault | `vault.connection.linked`/`refreshed`/`needs_reauth`/`revoked`, `vault.decrypt_failed`, `vault.master_key_rotated` |
| Authorization | `authz.denied`, `authz.shadow_deny`, `authz.allowed_critical` |
| Policy and system | `policy.changed` (diff), `bootstrap.claimed`, `owner.recovered_by_host`, `legacy.password_used`, `legacy.api_key_used`, `legacy.claimed_actor_ignored`, `audit.exported`, `audit.verified`, `audit.checkpoint` |

Egress requests go to a separate high-volume table, `identity_egress_log`, partitioned by day with 30-day retention. Only egress **denials** are copied into the security log.

**Every row records** `actor_principal_id`, `actor_kind`, `on_behalf_of`, `grant_id`, `chain`, `token_jti`, `credential_id`, `session_ref` (the session's id, never its token), `ip`, `user_agent`, `request_id`, `target_type`/`target_id`, `permission`, `scope`, `outcome` and `details` (structured JSON, never string-formatted).

### 8.3 What is never logged, anywhere

The following are never logged:
- passwords, including ones typed into the email field;
- TOTP codes and secrets, and recovery codes;
- session tokens, API keys (no prefix, no suffix), run tokens and egress credentials;
- provider access and refresh tokens, OAuth `code` and PKCE verifiers;
- SAML assertions, and id_tokens (only `iss`, `sub` and `jti` are logged);
- the master key and DEKs, and request or response bodies of authentication endpoints.

**The identifier for failed sign-ins on unknown accounts is stored only as an HMAC.** People sometimes type their password into the email box.

**One redaction filter covers all logging.** It is installed at the logging root and in error reporting. It redacts `Authorization`, `Cookie`, `Set-Cookie`, `X-Boss-CSRF`, and any key matching `token|secret|password|code|assertion|credential`. It also redacts values matching the BOSS key formats or JWT shape (`eyJ…\.…\.…`), and the query strings of OAuth and SSO callbacks. A test emits every credential shape and asserts that none survives (§15.3).

---

## 9. Console UI

There is one Identity area in Settings, replacing today's "Identity" (providers only) and "Access and packs". Packs move to Skills & MCP. A person-facing **My account** menu sits under the avatar in the top bar. Every screen follows the console UX rules: concise, one surface per job, short labels with tooltips, and e2e-tested. Mockups are in `~/Downloads/boss-identity-mockup/`, as HTML with a PNG beside each:

| Mockup | File |
|---|---|
| (a) Identity home | `identity-home` |
| (b) A person's profile | `person` |
| (c) Role editor | `role-editor` |
| (d) Delegation and lineage | `delegation` |
| (e) Sign-in, MFA and passkeys, plus claim, step-up and agent consent | `signin` |
| (f) Service accounts and keys | `service-accounts` and `service-accounts-key-shown-once` |
| (g) Security audit log | `audit` |

### 9.1 Identity › Overview (mockup a)
- **Posture strip** of six checks, each with a status and a one-click fix:
  - MFA coverage (by role);
  - stale API keys (>30 d unused or expiring soon);
  - risky delegations (grants with no expiry, broad scopes, or idle);
  - legacy credentials still in use (shared password, env API keys, classic GitHub PATs, network mode);
  - orphaned service accounts and agents without a sponsor;
  - the audit chain verified at a given time.
- **Directory panels:**
  - People: count by role, last-seen sparkline.
  - Groups.
  - Access roles: built-in plus custom.
  - Service accounts.
  - Agents: Passports by status.
- **Live column:** "Recent security events", with links into the audit log.

### 9.2 People and a person's profile (mockup b)
- **List:** search; filters for role, group, status, MFA and source (local/SSO/SCIM); bulk invite. "Invite people" takes emails, roles (limited to what you hold) and groups, then sends invitations or shows the links.
- **Profile tabs:**
  - Overview: identity, status, sign-in methods, SSO links, last sign-in.
  - **Access:** role bindings at org and per project, groups, and the **effective access** matrix by category, each permission with its *why* ("via Operator ← Builders group").
  - Sessions, with revoke.
  - Security: MFA factors, passkeys, recovery codes remaining, and admin actions (reset MFA, sign out everywhere, disable).
  - **Delegations they granted:** agents, scopes, expiry, last use, revoke.
  - API keys.
  - Audit, filtered to this person as actor or subject.

### 9.3 Groups
A list, and group detail: members, source badge (read-only for IdP groups), role bindings and their scopes, and the impact of a change.

### 9.4 Access roles and the role editor (mockup c)
- **List:** built-in roles (lock icon, *Clone*) and custom roles, with holder counts.
- **Editor:**
  - left: name, description and bindable scopes;
  - centre: the **permission matrix grouped by resource**. Rows are resources (Builds, Workflows, Runs…) and columns are actions. Cells are toggles. Locked cells you don't hold, critical cells in red, step-up marked with a key icon;
  - right: **Changes** (a +/- diff) and the **Impact preview**: "Affects 4 people (2 via *Builders*) · 1 service account · 3 agents through 5 grants · gains 2 pages, 14 API operations, 3 MCP tools".
- **Save:** step-up, then a confirmation that names the people affected.

### 9.5 Service accounts and API keys (mockup f)
- **List:** owner(s), roles, key count and the next expiry.
- **Create:** name, owners, roles (≤ yours), and a first key.
- **Key creation:** name, scopes (preset or custom picker), projects, CIDR, expiry. Then a **shown-once** dialog with copy-to-clipboard, a "stored it safely" checkbox before close, and the warning: *BOSS can't show this again*.
- **Rotate:** choose the overlap, and see both keys during the overlap.
- **Revoke.** Personal keys live under My account › API keys, with the same UI.

### 9.6 Agents and delegations (mockup d)
- **Agents list:** each Passport with its sponsor, ceiling preset, status, number of active grants and last run.
- **Passport page:** ceiling editor (the same matrix, limited to delegable permissions), external ceiling, spawn rules, depth, autonomous bindings, and lineage (born, sealed, changes).
- **Delegation and lineage view:**
  - a visual chain, **Person → Agent → Sub-agent**, where each hop shows its effective scope as chips that **narrow visibly from left to right**, with the dropped permissions struck through;
  - the grant card: expiry, projects, repos, sub-delegation rules;
  - the tokens minted, as a timeline;
  - the egress calls made;
  - **Revoke grant**, which first shows a **cascade preview**: "This halts 2 running builds, revokes 3 tokens, 1 GitHub token, pauses 1 schedule."
- **My account › Agents acting for me** is the person's own view: grants and connections, *Let an agent act for me*, and *Connect GitHub*.

### 9.7 Sign-in, MFA and passkey screens (mockup e)

The sign-in page follows the layout the operator chose, in BOSS tokens, fonts and the dark theme (D33). Top to bottom:
- **The BOSS mark** and a short welcome ("Welcome back" and one line).
- **A row of social and SSO icon buttons**, one per provider the Owner has configured: the OIDC presets Google, GitHub (OAuth 2.0), Microsoft and Apple, plus any enterprise OIDC or SAML provider (Okta, Entra, Keycloak…) by name. **Hidden entirely when none is configured.** One provider becomes a full-width "Continue with X" button.
- **A prominent "Sign in with a passkey"** primary button (hidden only when the browser has no WebAuthn).
- **"or use email"** divider.
- **Method tabs: Password | Magic link | Email code.** Password shows email, password and "Forgot password?". The two email tabs exist **only when SMTP is configured**; with no SMTP the tabs collapse to the password form and "Forgot password?" becomes "Ask an admin". Magic link shows "Check your email" with the uniform copy *"If an account exists for that address, we sent a link. It works once, for 10 minutes, in this browser."*; Email code shows a 6-digit entry with attempts left and Resend.
- **Footer:** "Ask an admin to invite you" by default; **"New here? Create account"** only when an Owner has turned on self sign-up (§4.15).
- **No phone or SMS anywhere.**

Other screens on the same sheet: **MFA** (TOTP entry with auto-advance, "Use a passkey instead", "Use a recovery code"), **Passkey** waiting state and errors, **Claim this BOSS** (bootstrap, including the network-bridge claim of §4.12), **Accept invitation**, **Enrol MFA** (QR, text secret once, confirm code), **Recovery codes** (shown once, download, print), the **Step-up sheet** and the **JIT consent sheet** for agents.

### 9.8 Sign-in and SSO settings
Providers (OIDC, SAML) with test sign-in, metadata and JWKS status, group mapping, JIT mode and domain lists. SCIM tokens. The security policy: MFA by role, session lengths, password policy, key expiry limits, delegation limits (max days, max depth, "until revoked" allowed), `github.require_user_repo_access`, and network mode.

### 9.9 Security audit log (mockup g)
- **Layout:** a filter bar (actor, kind, event group, outcome, time), a dense table and a detail drawer.
- **Detail drawer:** the full chain (Person → Agent → Agent), the grant, the token, the diff for changes, and the raw JSON with redaction already applied.
- **Header:** "Chain verified ✓ at 10:42 · seq 48,211". Export (step-up) and verify.

### 9.10 Access denied and empty states
Access denied names the missing permission and who can grant it, with a "Request access" button that creates a BOSS decision for the admins. When there are no other people, Identity Overview shows "You're the only person here — invite your team" instead of empty tables.

---

## 10. API surface

All identity routes live under `/v1/auth/*` (sign-in) and `/v1/identity/*` (everything else). IDENTITY_PERMISSIONS.md §5 lists each route with its declaration. The shapes:

```
# Sign-in (public, rate-limited, CSRF-checked)
POST /v1/auth/login                       {email, password}            → 200 {next: "mfa"|"done", methods[]} + __Host-boss_preauth
POST /v1/auth/mfa/totp                    {code}                       → 200 + __Host-boss_session
POST /v1/auth/mfa/recovery                {code}
POST /v1/auth/webauthn/options            {purpose: login|mfa|step_up} → PublicKeyCredentialRequestOptions
POST /v1/auth/webauthn/verify             {credential}
GET  /v1/auth/sso/{provider_id}/start     → 302 IdP
GET|POST /v1/auth/sso/{provider_id}/callback
POST /saml/v2/acs                         (SAML POST binding; org from the SP entity id)
POST /v1/auth/password/forgot             {email}                      → 202 always
POST /v1/auth/magic-link                  {email}                      → 202 always (SMTP only; §4.14)
POST /v1/auth/magic-link/verify           {token}                      (same browser; MFA roles → next: "mfa")
POST /v1/auth/email-code                  {email}                      → 202 always (SMTP only)
POST /v1/auth/email-code/verify           {code}
POST /v1/auth/signup                      {name, email}                → 202 always; 404 when self sign-up is off (§4.15)
POST /v1/auth/password/reset              {token, new_password}
POST /v1/auth/setup/claim                 {setup_token|legacy_password, name, email, password}
POST /v1/auth/invitations/{token}/accept  {name, password} | via SSO
POST /v1/auth/step-up                     (passkey or TOTP; password only for users without MFA)
POST /v1/auth/logout
GET  /v1/auth/session                     → {principal, org, mfa_at, csrf, expires}   (replaces today's)

# Me
GET  /v1/identity/me                      → profile, sign-in methods, csrf token, epoch
GET  /v1/identity/me/permissions?project_id=   → {epoch, permissions[], step_up_fresh_until}
POST/DELETE /v1/identity/me/mfa/totp      (enrol: returns otpauth URI ONCE; confirm with code)
GET/POST/PATCH/DELETE /v1/identity/me/passkeys
POST /v1/identity/me/recovery-codes       → codes ONCE
POST /v1/identity/me/password             {current, new}
GET/DELETE /v1/identity/me/sessions[/{id}]
GET/POST/DELETE /v1/identity/me/identities   (link and unlink SSO)
GET/POST /v1/identity/me/api-keys · POST …/{id}/rotate · POST …/{id}/revoke
GET/POST/DELETE /v1/identity/me/connections  (vault: connect GitHub, Google…; OAuth redirect flows)
GET/POST /v1/identity/me/delegations · POST …/{id}/revoke · POST …/preview  (cascade and scope preview)

# Administration
GET/POST /v1/identity/users · GET/PATCH/DELETE /v1/identity/users/{id}
POST /v1/identity/users/{id}/{disable|enable|reset-mfa|revoke-sessions|reset-link}
GET  /v1/identity/principals/{id}/effective?project_id=&permission=   → permissions with why-chains
GET/POST /v1/identity/groups · GET/PATCH/DELETE …/{id} · POST/DELETE …/{id}/members
GET/POST /v1/identity/access-roles · GET/PATCH/DELETE …/{id} · POST …/{id}/clone
POST /v1/identity/access-roles/{id}/preview  {permissions[]} → {diff, affected{people,groups,service_accounts,agents,grants}, pages±, routes±, tools±}
GET  /v1/identity/access-roles/{id}/versions
GET/POST /v1/identity/role-bindings · DELETE …/{id}
GET/POST /v1/identity/service-accounts · GET/PATCH/DELETE …/{id} · keys: GET/POST …/{id}/keys · POST …/keys/{key_id}/{rotate|revoke}
GET  /v1/identity/api-keys · POST …/{id}/revoke
GET  /v1/identity/sessions · DELETE …/{id}
GET  /v1/identity/agents · GET/PATCH /v1/identity/agents/{agent_id}/passport · POST …/{suspend|resume|revoke} · GET …/lineage
GET  /v1/identity/delegations · GET …/{id} (chain, tokens, egress) · POST …/{id}/revoke
GET  /v1/identity/connections · POST …/{id}/revoke
GET/POST /v1/identity/providers · GET/PATCH/DELETE …/{id} · POST …/{id}/test · scim keys: POST/DELETE …/{id}/scim-keys
GET/PUT /v1/identity/policy
GET  /v1/identity/posture
GET  /v1/identity/audit?actor=&kind=&event=&outcome=&from=&to=&cursor= · GET …/{id}
POST /v1/identity/audit/export · POST /v1/identity/audit/verify
GET  /v1/identity/catalogue                → the generated catalogue (for the console and docs)
GET  /v1/identity/seats · PUT …/{seat_key}  (approval seats, approval_rule:manage)

# Tokens (agents)
POST /v1/identity/token                    RFC 8693 token exchange (run and actor tokens only)
POST /v1/identity/introspect               egress service credential only
POST /v1/identity/credentials/github       JIT fallback (policy-gated)
GET  /.well-known/boss-jwks.json
```

**Errors**
- `401 {"error":"unauthenticated"}`
- `401 {"error":"step_up_required"}`
- `403 {"error":"forbidden","permission":"…"}`
- `404` for invisible objects
- `409` for last-Owner or role-in-use
- `422` for unknown permission keys and mass assignment
- `429` with `Retry-After`

Error bodies never contain stack traces or exception text. Returning exception text is a common flaw.

---

## 11. Database schema

Every table is `identity_`-prefixed, idempotent (`CREATE TABLE IF NOT EXISTS`) in `db/schema.sql`, and scoped by `org_id`. Text ids carry a kind prefix. Timestamps are `timestamptz`.

**The frozen DDL is the `BOSS IDENTITY` block of `db/schema.sql` (Phase 0).** The sketch below was its input; where they differ the schema file wins, and IDENTITY_BUILD.md §7 lists each difference (no CITEXT, providers and legacy-mapping tables, the KEK table, the aggregated shadow table, the DPoP replay cache, the triggers).

```sql
CREATE TABLE identity_principals (
  id TEXT PRIMARY KEY,                       -- usr_/sva_/agt_/sys_
  org_id TEXT NOT NULL REFERENCES orgs(id),
  kind TEXT NOT NULL CHECK (kind IN ('user','service_account','agent','system')),
  status TEXT NOT NULL CHECK (status IN ('pending','active','disabled','suspended','revoked')),
  display_name TEXT NOT NULL,
  network_bound BOOLEAN NOT NULL DEFAULT false,      -- usr_network_operator
  legacy_member_ids TEXT[] NOT NULL DEFAULT '{}',    -- 'ceo','executive_console',… for history mapping
  created_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  status_changed_at TIMESTAMPTZ, status_changed_by TEXT, status_reason TEXT
);
CREATE INDEX ON identity_principals (org_id, kind, status);

CREATE TABLE identity_users (
  principal_id TEXT PRIMARY KEY REFERENCES identity_principals(id),
  email CITEXT NOT NULL, email_verified_at TIMESTAMPTZ,
  password_hash TEXT,                        -- PHC argon2id; NULL = no password (SSO/passkey only)
  password_changed_at TIMESTAMPTZ,
  mfa_required BOOLEAN NOT NULL DEFAULT false,   -- per-user override; role policy also applies
  last_login_at TIMESTAMPTZ, avatar_seed TEXT,
  UNIQUE (email)                             -- one org per install today; becomes (org_id,email) if multi-org
);

CREATE TABLE identity_user_identities (      -- SSO links
  id TEXT PRIMARY KEY, principal_id TEXT NOT NULL REFERENCES identity_principals(id),
  provider_id TEXT NOT NULL, issuer TEXT NOT NULL, subject TEXT NOT NULL,
  email_at_link CITEXT, linked_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_used_at TIMESTAMPTZ,
  UNIQUE (issuer, subject)
);

CREATE TABLE identity_totp (
  principal_id TEXT PRIMARY KEY REFERENCES identity_principals(id),
  vault_item_id TEXT NOT NULL,               -- encrypted secret lives in the vault
  last_step BIGINT NOT NULL DEFAULT 0, enrolled_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE identity_webauthn_credentials (
  id TEXT PRIMARY KEY, principal_id TEXT NOT NULL REFERENCES identity_principals(id),
  credential_id BYTEA NOT NULL UNIQUE, public_key BYTEA NOT NULL, sign_count BIGINT NOT NULL,
  aaguid UUID, transports TEXT[], backup_eligible BOOLEAN, backup_state BOOLEAN,
  name TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_used_at TIMESTAMPTZ
);
CREATE TABLE identity_recovery_codes (
  id TEXT PRIMARY KEY, principal_id TEXT NOT NULL REFERENCES identity_principals(id),
  code_hash TEXT NOT NULL, used_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE identity_auth_challenges (      -- pre-auth, MFA, WebAuthn, OIDC state, reset, invite, setup
  id_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, principal_id TEXT,
  binding_hash TEXT,                         -- hash of the pre-auth cookie it is bound to
  payload_enc BYTEA,                         -- nonce/PKCE verifier/request id, encrypted
  attempts INT NOT NULL DEFAULT 0, expires_at TIMESTAMPTZ NOT NULL, consumed_at TIMESTAMPTZ
);
CREATE TABLE identity_sessions (
  id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
  principal_id TEXT NOT NULL REFERENCES identity_principals(id),
  auth_methods TEXT[] NOT NULL, mfa_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  idle_expires_at TIMESTAMPTZ NOT NULL, absolute_expires_at TIMESTAMPTZ NOT NULL,
  ip INET, user_agent TEXT, device_id_hash TEXT,
  revoked_at TIMESTAMPTZ, revoked_by TEXT, revoked_reason TEXT, replaced_by TEXT
);
CREATE INDEX ON identity_sessions (principal_id) WHERE revoked_at IS NULL;
CREATE TABLE identity_invitations (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, email CITEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
  bindings JSONB NOT NULL DEFAULT '[]', group_ids TEXT[] NOT NULL DEFAULT '{}',
  invited_by TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL, accepted_at TIMESTAMPTZ,
  accepted_principal_id TEXT, revoked_at TIMESTAMPTZ
);

CREATE TABLE identity_groups (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, key TEXT NOT NULL, name TEXT NOT NULL, description TEXT,
  source TEXT NOT NULL CHECK (source IN ('manual','scim','oidc')), external_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (org_id, key)
);
CREATE TABLE identity_group_members (
  group_id TEXT NOT NULL REFERENCES identity_groups(id), principal_id TEXT NOT NULL REFERENCES identity_principals(id),
  source TEXT NOT NULL, added_by TEXT, added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (group_id, principal_id)
);

CREATE TABLE identity_permissions (          -- seeded from the catalogue at boot; never written otherwise
  key TEXT PRIMARY KEY CHECK (key ~ '^[a-z][a-z_]*:[a-z][a-z_]*$'),
  resource TEXT NOT NULL, action TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
  category TEXT NOT NULL, scope_kind TEXT NOT NULL, step_up BOOLEAN NOT NULL, delegable BOOLEAN NOT NULL,
  critical BOOLEAN NOT NULL, system_only BOOLEAN NOT NULL, catalogue_version INT NOT NULL
);
CREATE TABLE identity_access_roles (
  id TEXT PRIMARY KEY, org_id TEXT,          -- NULL = built-in
  key TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  builtin BOOLEAN NOT NULL, bindable_scopes TEXT[] NOT NULL, version INT NOT NULL DEFAULT 1,
  created_by TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (org_id, key)
);
CREATE TABLE identity_access_role_permissions (
  role_id TEXT NOT NULL REFERENCES identity_access_roles(id) ON DELETE CASCADE,
  permission_key TEXT NOT NULL REFERENCES identity_permissions(key),
  PRIMARY KEY (role_id, permission_key)
);
CREATE TABLE identity_access_role_versions (
  role_id TEXT NOT NULL, version INT NOT NULL, permissions TEXT[] NOT NULL, changed_by TEXT NOT NULL,
  changed_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (role_id, version)
);
CREATE TABLE identity_role_bindings (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL,
  subject_type TEXT NOT NULL CHECK (subject_type IN ('principal','group')), subject_id TEXT NOT NULL,
  role_id TEXT NOT NULL REFERENCES identity_access_roles(id),
  scope_type TEXT NOT NULL CHECK (scope_type IN ('org','project')), scope_id TEXT NOT NULL,
  granted_by TEXT NOT NULL, granted_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ, reason TEXT,
  UNIQUE (subject_type, subject_id, role_id, scope_type, scope_id)
);
-- last-owner guard: CONSTRAINT TRIGGER identity_last_owner AFTER INSERT OR UPDATE OR DELETE
--   ON identity_role_bindings / identity_principals DEFERRABLE INITIALLY DEFERRED → raises if an org has 0 active owners

CREATE TABLE identity_service_accounts (
  principal_id TEXT PRIMARY KEY REFERENCES identity_principals(id), description TEXT,
  owner_principal_ids TEXT[] NOT NULL CHECK (cardinality(owner_principal_ids) >= 1)
);
CREATE TABLE identity_api_credentials (
  id TEXT PRIMARY KEY,                       -- key_… public id
  principal_id TEXT NOT NULL REFERENCES identity_principals(id),
  kind TEXT NOT NULL CHECK (kind IN ('pat','sak','scim','legacy_env')),
  name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
  scopes TEXT[] NOT NULL, project_ids TEXT[], cidrs CIDR[],
  expires_at TIMESTAMPTZ, created_by TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at TIMESTAMPTZ, last_used_ip INET, last_used_ua TEXT,
  rotated_from TEXT, grace_until TIMESTAMPTZ, suspended_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ, revoked_by TEXT
);

CREATE TABLE identity_agent_passports (
  agent_id TEXT PRIMARY KEY REFERENCES identity_principals(id),   -- = roles.agent_id
  role_key TEXT NOT NULL, sponsor_principal_id TEXT NOT NULL, version INT NOT NULL DEFAULT 1,
  can_spawn BOOLEAN NOT NULL DEFAULT false, allowed_children TEXT[] NOT NULL DEFAULT '{}',
  max_depth INT NOT NULL DEFAULT 3 CHECK (max_depth BETWEEN 0 AND 5),
  autonomous BOOLEAN NOT NULL DEFAULT false, external_ceiling JSONB NOT NULL DEFAULT '{}',
  reissued_from TEXT, sealed_fingerprint TEXT
);
CREATE TABLE identity_agent_ceilings (
  agent_id TEXT NOT NULL REFERENCES identity_agent_passports(agent_id),
  permission_key TEXT NOT NULL REFERENCES identity_permissions(key),   -- trigger: must be delegable
  PRIMARY KEY (agent_id, permission_key)
);

CREATE TABLE identity_delegation_grants (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL,
  user_principal_id TEXT NOT NULL REFERENCES identity_principals(id),
  agent_ids TEXT[] NOT NULL, project_ids TEXT[] NOT NULL, resources JSONB NOT NULL DEFAULT '{}',
  allow_subdelegation BOOLEAN NOT NULL DEFAULT false, allowed_children TEXT[] NOT NULL DEFAULT '{}',
  max_depth INT NOT NULL DEFAULT 3, task_run_id TEXT,            -- set = task-bound ("this build only")
  purpose TEXT, parent_grant_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('active','suspended','revoked','expired')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ,
  last_used_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ, revoked_by TEXT, revoke_reason TEXT
);
CREATE TABLE identity_delegation_grant_permissions (
  grant_id TEXT NOT NULL REFERENCES identity_delegation_grants(id),
  permission_key TEXT NOT NULL REFERENCES identity_permissions(key),
  PRIMARY KEY (grant_id, permission_key)
);
CREATE TABLE identity_run_credentials (     -- one row per minted run token (not the token)
  jti TEXT PRIMARY KEY, parent_jti TEXT, run_id TEXT NOT NULL, grant_id TEXT,
  subject_principal_id TEXT NOT NULL, mode TEXT NOT NULL, chain JSONB NOT NULL, depth INT NOT NULL,
  aud TEXT NOT NULL, scope_hash TEXT NOT NULL, cnf_jkt TEXT NOT NULL,
  issued_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL, revoked_at TIMESTAMPTZ
);
CREATE TABLE identity_egress_credentials (
  id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL, sandbox_id TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL, revoked_at TIMESTAMPTZ
);

CREATE TABLE identity_vault_items (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, owner_principal_id TEXT,   -- NULL = org-level (GitHub App key)
  kind TEXT NOT NULL,                        -- oauth_user | app_private_key | provider_credential | oidc_client_secret | totp_secret | legacy_pat
  provider TEXT, provider_subject TEXT, granted_scopes TEXT[],
  dek_wrapped BYTEA NOT NULL, kek_id TEXT NOT NULL,
  secret_enc BYTEA NOT NULL, secret_nonce BYTEA NOT NULL,             -- access token / key / secret
  refresh_enc BYTEA, refresh_nonce BYTEA, access_expires_at TIMESTAMPTZ, refresh_expires_at TIMESTAMPTZ,
  status TEXT NOT NULL CHECK (status IN ('active','needs_reauth','revoked')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_used_at TIMESTAMPTZ, last_refreshed_at TIMESTAMPTZ
);
CREATE TABLE identity_signing_keys (
  kid TEXT PRIMARY KEY, family TEXT NOT NULL CHECK (family IN ('run','actor')), alg TEXT NOT NULL,
  public_jwk JSONB NOT NULL, private_vault_item_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('next','active','retiring','retired')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), activated_at TIMESTAMPTZ, retire_after TIMESTAMPTZ
);
CREATE TABLE identity_approval_seats (
  org_id TEXT NOT NULL, seat_key TEXT NOT NULL, principal_id TEXT NOT NULL,
  assigned_by TEXT NOT NULL, assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, seat_key, principal_id)
);
CREATE TABLE identity_settings (
  org_id TEXT PRIMARY KEY, policy JSONB NOT NULL, epoch BIGINT NOT NULL DEFAULT 0,
  updated_by TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNLOGGED TABLE identity_rate_limits (
  bucket TEXT NOT NULL, key_hash TEXT NOT NULL, window_start TIMESTAMPTZ NOT NULL, count INT NOT NULL,
  PRIMARY KEY (bucket, key_hash, window_start)
);

CREATE TABLE identity_security_events (
  id BIGSERIAL PRIMARY KEY, org_id TEXT NOT NULL, seq BIGINT NOT NULL, ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  stream TEXT NOT NULL CHECK (stream IN ('security','lineage')),
  event_type TEXT NOT NULL, outcome TEXT NOT NULL CHECK (outcome IN ('success','failure','denied','info')),
  actor_principal_id TEXT, actor_kind TEXT, on_behalf_of TEXT, grant_id TEXT, chain JSONB,
  token_jti TEXT, credential_id TEXT, session_ref TEXT,
  target_type TEXT, target_id TEXT, permission TEXT, scope TEXT,
  ip INET, user_agent TEXT, request_id TEXT, details JSONB NOT NULL DEFAULT '{}',
  prev_hash BYTEA NOT NULL, hash BYTEA NOT NULL, UNIQUE (org_id, seq)
);
-- REVOKE UPDATE, DELETE ON identity_security_events FROM boss_app; trigger raises on UPDATE/DELETE.
CREATE INDEX ON identity_security_events (org_id, ts DESC);
CREATE INDEX ON identity_security_events (org_id, actor_principal_id, ts DESC);
CREATE INDEX ON identity_security_events (org_id, grant_id) WHERE grant_id IS NOT NULL;
CREATE TABLE identity_egress_log (...)      -- PARTITION BY RANGE (ts), daily; 30-day retention
```

**Existing tables after migration**
- `project_members` and `org_members` become **read-only history**. Their rows map to principals through `legacy_member_ids`.
- `break_glass_grants` stays, as time-boxed role bindings (`expires_at`).
- `enterprise_saml_sessions` is retired.
- `enterprise_scim_users` and `enterprise_scim_groups` feed `identity_users` and `identity_groups`.
- `github_connections.credential_ref` moves to `identity_vault_items`.
- `roles` (agent roles) is unchanged. Its `agent_id` is the Passport principal.

---

## 12. Migration from today

Two guarantees hold throughout:
1. **No step locks out an existing install.**
2. **Every step can be reversed with one setting until the step that removes the legacy path.**

| Step | What happens on upgrade | What the operator sees | Rollback |
|---|---|---|---|
| **M0 Schema and catalogue** | Identity tables created. Catalogue seeded. Every legacy member (`ceo`, `cto`, `cao`, `vp_agents`, `release_manager`) becomes a **pending placeholder** person with `legacy_member_ids`. `executive_console` and `metatron`/`coo`/`system` become **system** principals with a fixed Owner-equivalent set, used only by legacy credentials. Seats are seeded from `default_safety.yaml`. Passports get principal rows; sponsor = the claimed Owner, set at M1. | Nothing changes. | Drop the tables. |
| **M1 Claim** | The console shows **"Claim this BOSS"** to anyone who signs in with the legacy password (or the setup token): create your account and enrol MFA. That person becomes the first Owner, and can "be" a legacy seat such as `ceo`, so history and approvals map to them. | A one-time claim flow. | Not needed: the legacy password keeps working. |
| **M2 Shadow** | The PDP runs on every route in **shadow mode**. Legacy credentials map to legacy principals: the shared password maps to `usr_legacy_shared` with the **Operator** role (D25); `BOSS_API_KEY` maps to `sys_legacy_console` (Owner-equivalent); `BOSS_API_KEYS` with `BOSS_API_KEY_PRINCIPALS` are hashed into `api_credentials(kind=legacy_env)` bound to the mapped principals. The body `actor` is **ignored for authorization** and logged when it differs from the principal (`legacy.claimed_actor_ignored`). | Identity → Overview lists every legacy path still in use, with counts and last use, and a **shadow-deny report** per router. | `BOSS_AUTHZ_MODE=off`. |
| **M3 People** | The Owner invites people (or wires SSO/SCIM). The console signs in with **sessions**. Server-side, it stops using `BOSS_API_KEY` and forwards the person's cookie. MCP clients are told to switch to PATs: a banner, plus `legacy.api_key_used` counts per client. | A banner on the legacy shared password: "*N* people still use the shared password: invite them." | Legacy paths still work. |
| **M4 Enforce** | Routers flip to `enforce` one by one once their shadow denials are zero for 7 days or explained. When all are flipped, the global mode becomes `enforce`. Agents get run tokens. The worker moves to `sys_worker` plus job run tokens, and `BOSS_WORKER_PRINCIPAL` maps to it. | Shadow report → green. | Per-router flip back to shadow. |
| **M5 Retire legacy** | **The legacy password stops working** 30 days after the claim (D25; an Owner may extend it once, by at most 30 days, step-up, audited). `BOSS_API_KEY` stops being accepted after the same date; nothing keeps it longer. A person's env API keys stop at the same date; the worker's keeps working. Host tools move to the `Host tools` service account's key (`BOSS_HOST_API_KEY`). | A countdown banner on every page for the whole window, and `Deprecation`/`Sunset` headers on legacy responses. | Extend the date once (Owner, step-up). |

**Network-trusted installs** (D27, §4.12)
- At M0, `usr_network_operator` is created with the Owner-equivalent bridge set, so behaviour is identical to today. It carries the network banner and posture flag, and it can claim.
- At M1, the operator claims a real account through the network-bridge claim: origin confirmed, passkey registered. No env change and no log access are needed.
- From the claim, the bridge maps to Operator and **ends 30 days later** (or at **Require sign-in**, whichever is first). There is no permanent network operator and no proxy-header identity.

**SSO and SCIM pieces that exist today**
- Configured OIDC and SAML providers are imported into the new provider model, with the same issuer, client and certificate.
- Existing OIDC-bearer API callers keep working as API callers until M5. From M3, the same providers also offer **console sign-in**.
- SCIM users and groups are imported as `pending` people and `scim` groups. Each SCIM bearer token is rehashed into a `boss_scim_` key bound to a SCIM system principal. The old raw tokens keep working until they are rotated, and are flagged.

**History.** Old rows keep their claimed actor strings. The activity log renders them through `legacy_member_ids`, so `ceo` is shown as "Param (was: ceo)". Nothing is rewritten.

---

## 13. Build plan

### 13.1 Phase 0: freeze the interfaces (1 week, one owner, reviewed by the operator)

**Done on branch `id/identity`; what was frozen, where, and the amendments it needed are in [IDENTITY_BUILD.md](../maintainers/IDENTITY_BUILD.md). The build runs as the five streams defined there (S1–S5), which supersede the A–J table in §13.2.**

These are frozen and committed before any stream starts. Once frozen, a change needs a written amendment:

1. `packages/identity/catalogue.yaml` v1, plus generators (Python, TS, SQL, docs). `IDENTITY_PERMISSIONS.md` becomes generated output.
2. `boss_engine.identity` **public interfaces**, as typed stubs with docstrings:
   - `Principal` and `AuthContext`;
   - `authorize(ctx, permission, scope) -> Decision`;
   - `permit`, `public` and `authenticated` decorators, and `Scope` resolvers;
   - `current_principal` dependency;
   - `emit_security_event(...)`;
   - `vault.get_token(owner, provider, purpose)`;
   - `token_service.mint_run_token(...)` and `.exchange(...)`.
3. The DDL in §11, as an idempotent block in `db/schema.sql`.
4. The OpenAPI for `/v1/auth/*` and `/v1/identity/*`, with request and response schemas.
5. Token formats and claims (§5.1, §7.5). JWKS path. Cookie names and the CSRF header.
6. Console contracts: `<Can>`, `useCan`, `guardedAction`, `AreaDef.permission`, `<AccessDenied>`, `<StepUpSheet>`, `<ConsentSheet>`.
7. Test harness contracts: fixture principals per role, `as_role("operator")`, `run_token_for(...)`, and the matrix YAML format.

### 13.2 Streams (parallel after Phase 0)

| Stream | Scope | Depends on (frozen) | Independent tester checks |
|---|---|---|---|
| **A · Sign-in core** | users, argon2id, sessions, CSRF, TOTP, passkeys, recovery, step-up, invitations, claim, rate limits, SSRF client | DDL, OpenAPI, events | §15.2 J1–J6, adversarial A1–A20 |
| **B · PDP and catalogue** | generators, `authorize`, the epoch cache, `GuardedRoute`, shadow mode, scope resolvers, MCP `ToolSpec.permission` | catalogue, interfaces | unit tests, matrix |
| **C · Route declarations** | `@permit` on all ~1,144 routes, split into 6 sub-streams by router file (Factory/GitHub, Workflows/Runs, Agents/Skills/MCP, Deploy/Infra, Models/Spend/Policy, Ops/Assurance/Activity). Plus composite-view filtering | B's decorator stubs (can start against stubs) | route-coverage and cross-project tests |
| **D · Credentials** | PATs, service accounts, keys, rotation, legacy env import, SCIM keys | DDL, events | A21–A30 |
| **E · Agent identity** | Passport upgrade, ceilings, grants, consent, token service, run tokens, DPoP shim, sub-agent exchange, job principal propagation, cascade | catalogue, interfaces, PDP stub | J8–J10, A31–A50 |
| **F · Vault and egress** | envelope vault, master key, GitHub App per-run tokens, user OAuth, user-repo-access guard, `boss-egress`, JIT fallback | vault interface, run-token verify | A51–A60 |
| **G · SSO and SCIM** | OIDC RP, SAML SP, linking, JIT, group sync, enforce-SSO, SCIM rewire | sign-in core session API | J7, A61–A72 |
| **H · Audit** | security events, hash chain, verify, export, redaction filter, egress log | events interface | A73–A80 |
| **I · Console** | Identity area (8 pages), My account, sign-in screens, `<Can>` across all areas, impact preview, lineage view | OpenAPI, console contracts; mock server from OpenAPI | Playwright journeys; independent tester walks every screen |
| **J · Migration** | M0–M5, legacy principals, posture items, network mode, CLI commands | everything above (integrates last) | migration rehearsal on a production-like snapshot |

### 13.3 Phases (each ends with a release candidate)

| Phase | Ships | Exit criteria |
|---|---|---|
| **P1 · Real people** | A, D (PATs), H, J M0–M3, B in **shadow**, I: sign-in, My account, People, Overview | Claim works on a fresh install and on a production-like snapshot rehearsal. The shared password still works. Every sign-in is audited. The shadow-deny report renders. |
| **P2 · Enforce** | B and C complete, roles, groups, custom roles, impact preview, service accounts, the full console `<Can>` | Route-coverage tests pass with **zero** undeclared routes, tools or areas. The matrix test is green. The cross-project 404 suite is green. Every router in enforce. M4. |
| **P3 · Delegation** | E, F | An agent run uses no shared key. GitHub writes go only through egress. Revocation cascade ≤5 s. The prompt-injection exfiltration test finds no token. |
| **P4 · SSO console sign-in** | G | OIDC (Keycloak, Okta, Entra, Google) and SAML (Okta, Entra) sign-in pass. SCIM deprovisioning cascades. |
| **P5 · Hardening and AS** | legacy retirement (M5), external KMS, OAuth AS for MCP (PKCE, device flow), GitHub Actions OIDC federation | External security review (OSS-funded or community) with no open highs. |

---

## 14. Threat model (STRIDE per component)

S = spoofing, T = tampering, R = repudiation, I = information disclosure, D = denial of service, E = elevation of privilege. Test ids refer to §15.3.

### 14.1 Console and Next.js proxy
| | Threat | Mitigation | Test |
|---|---|---|---|
| S | A cross-site request rides the session (CSRF) | SameSite=Lax, an Origin match, `Sec-Fetch-Site`, and a per-session `X-Boss-CSRF` HMAC | A8 |
| T | The proxy forwards client-set identity headers | The proxy strips every `X-Boss-*` and identity header before forwarding. The API ignores any it receives. | A9 |
| I | Session cookie stolen through XSS | HttpOnly, `__Host-`, and a strict CSP (no inline scripts except nonce-ed ones). Tokens are never in JS or URLs. | A10 |
| E | Hidden UI used as authorization | The API re-checks everything. The Viewer walk asserts that no mutating 2xx occurs. | J11 |

### 14.2 Sign-in service (passwords, MFA, passkeys, recovery)
| | Threat | Mitigation | Test |
|---|---|---|---|
| S | Credential stuffing or brute force | Per-IP and per-account limits, a soft lock exempting familiar devices, the blocklist | A1, A2 |
| S | MFA brute force or code replay | 5 attempts per challenge, the last-step replay guard | A4, A5 |
| S | Passkey challenge replayed or bound to another user | Single-use, hashed, cookie-bound challenges. The user handle must match. | A6 |
| S | Account recovery abused (host header, token reuse) | `BOSS_PUBLIC_URL` links, hashed single-use 30-minute tokens, no MFA bypass | A11, A12 |
| T | Session fixation | Rotation on sign-in, MFA and step-up. Pre-auth cookies are never promoted. | A7 |
| R | "I never signed in" | Every sign-in is audited with IP, user agent, device and method | A73 |
| I | Account enumeration by response or timing | Identical bodies, a dummy hash, disabled status revealed only after the correct password | A3 |
| D | Argon2 memory exhaustion | A semaphore of 4 and a 2 s queue, then 429 | A13 |
| E | Bootstrap reused after setup | The setup endpoint is 404 for good once an Owner exists | A14 |

### 14.3 SSO and SCIM
| | Threat | Mitigation | Test |
|---|---|---|---|
| S | Takeover by linking on an unverified email (pre-account takeover) | Link only by `(iss, sub)`. Email binds only to an unclaimed invitation, with `email_verified=true`. | A61 |
| S | Forged or unsigned id_token, alg confusion, `none` | JWKS signature check, an alg allowlist, `iss`/`aud`/`azp`/`nonce` | A62–A64 |
| S | IdP mix-up between providers | An exact issuer match in discovery, plus RFC 9207 `iss` | A65 |
| S | SAML signature wrapping, replay, unsolicited responses | Strict library, signed assertions required, `InResponseTo`, an id replay cache, IdP-initiated off | A66–A68 |
| T | Login CSRF | `state` bound to the pre-auth cookie | A69 |
| I | SSRF through discovery, JWKS or metadata URLs | The SSRF-safe client, private ranges blocked, redirects pinned | A70, A71 |
| E | SCIM token used beyond provisioning | `scim:provision` only. SCIM cannot assign roles. | A72 |

### 14.4 Credentials (API keys, service accounts)
| | Threat | Mitigation | Test |
|---|---|---|---|
| S | A leaked key is used from elsewhere | Expiry, CIDR allowlists, new-IP events, a scanner-friendly format, rotation | A21–A23 |
| I | A key revealed in the UI, logs or support bundles | Shown once and never displayed again. The redaction filter covers the key formats. | A24, A80 |
| E | Minting a key for a more powerful service account | The creator must hold ⊇ the service account's permissions | A25 |
| E | A key used for step-up actions | Bearer credentials cannot step up | A26 |
| T | A timing oracle on key lookup | Lookup by hash, constant-time compare | A27 |

### 14.5 Authorization (PDP, route guards, catalogue)
| | Threat | Mitigation | Test |
|---|---|---|---|
| E | An undeclared route is reachable | `GuardedRoute` refuses to boot, plus the coverage test | R1 |
| E | IDOR or BOLA across projects | Scope resolved from the object, 404 for invisible objects, the cross-project suite | R3 |
| E | Self-escalation through role, group or custom-role edits | The subset rule, no self-binding, `owner:assign` | A31–A35 |
| E | Mass assignment | Allowlisted schemas, 422 on extra fields | A36 |
| T | A stale permission cache after demotion | Epoch plus NOTIFY, a 5 s TTL cap | A37 |
| D | The last Owner removed in a race | A deferred DB constraint and an advisory lock | A38 |
| R | Denials invisible | `authz.denied` always audited | A73 |

### 14.6 Token service and run tokens
| | Threat | Mitigation | Test |
|---|---|---|---|
| S | A token replayed outside its sandbox | DPoP `cnf.jkt` held by the shim, audience-bound, 5-minute lifetime, egress firewall | A41, A42 |
| S | Cross-token confusion (a session or run token used as another kind) | Distinct `typ`, `aud` and key families. Sessions are opaque. | A43 |
| T | A forged chain or depth in the request | The chain is assembled only from verified tokens. The depth is the minimum along the chain. Cycles are rejected. | A44–A46 |
| E | A child wider than its parent, or empty scope treated as "everything" | `new = requested ∩ parent ∩ ceiling`, and empty stays empty | A47, A48 |
| E | An agent self-approves or self-grants | Non-delegable permissions, rejected at mint and at the PDP | A49, A50 |
| E | Delegation to an arbitrary agent | The grant names its agents, plus `can_spawn` and `allowed_children` | A39 |
| I | Introspection leaks | Not public. Egress credential only. It returns `act`. | A40 |
| D | Exchange flooding | 60/min per run | A13b |

### 14.7 Vault and egress proxy
| | Threat | Mitigation | Test |
|---|---|---|---|
| I | The agent exfiltrates a GitHub token (prompt injection) | The token never enters the sandbox: the proxy injects it. JIT is off by default and audited. | A51 |
| I | A DB dump exposes provider tokens | Envelope AES-256-GCM, the KEK outside the DB, AAD row binding | A52, A53 |
| E | Confused deputy: run R uses Bob's connection or another repo | Vault keyed by the token's `sub`, never by a request field. The proxy checks the run's repos. The user-repo-access guard. | A54, A55 |
| E | The agent pushes to main or edits workflows | Ref rules on receive-pack, `.github/workflows` writes blocked | A56 |
| T | Provider refresh-token race loses the token | Single-flight row lock, `needs_reauth` | A57 |
| D | Master key lost | The installer forces a backup acknowledgement. The KCV boot check. A documented recovery: reconnect providers. | M-doc |
| R | Egress calls unattributed | `egress_log` with the chain for every request | A58 |

### 14.8 Audit log
| | Threat | Mitigation | Test |
|---|---|---|---|
| T | Rows altered or deleted | INSERT-only grants, a trigger, the hash chain, daily anchors, verify | A74, A75 |
| R | An event write silently fails | Same transaction as the change. If the write fails, the change fails. | A76 |
| I | Secrets in the log | The never-log list and the redaction filter | A80 |
| E | A forged audit row through the API | No write API exists | A77 |

### 14.9 Migration and legacy paths
| | Threat | Mitigation | Test |
|---|---|---|---|
| E | The legacy shared password keeps full power indefinitely | Mapped to a chosen role (default Operator). The countdown. Posture. | M1–M3 |
| S | Network mode trusted from anywhere | It already requires a private network. Banner, IP audit, posture red. Optional proxy identity only from loopback. | M4 |
| E | A claimed `actor` still honoured somewhere | Actors are ignored for authorization. A grep test finds `payload.actor` used in authorization calls. | R6 |

---

## 15. Test plan

Per BOSS practice:
- **During development**, run the tests the change touches.
- **As the final gate**, run the full suites once.
- **An independent agent tests every screen** in a browser before anything is reported.

### 15.1 Unit tests (fast, in the engine and the API)
- **Argon2id:** the PHC parameters are as specified; rehash-on-login when parameters change; length limits; NFKC normalisation; blocklist hits.
- **TOTP:** the window, the replay guard, attempt counting; the secret is never stored outside the vault.
- **WebAuthn:** verification with fixture authenticators (UV required, user-handle binding, counter regression event).
- **Session:** rotation, idle and absolute expiry, revocation, the CSRF HMAC.
- **PDP:**
  - union of bindings, group bindings, project versus org scope, org-only permissions refused at project scope;
  - key scopes ∩;
  - OBO intersection;
  - autonomous mode;
  - not-delegable denial;
  - step-up;
  - `explain()` output.
- **Token service:** each check of §7.5 in isolation (signature, alg, kid, typ, aud, exp/nbf skew, DPoP htm/htu/iat/jti/ath, depth min, cycles, scope intersection, empty scope).
- **Vault:** round-trip; AAD swap fails; KCV mismatch blocks boot; no plaintext fallback.
- **Catalogue generators:** deterministic output; unknown keys rejected; no wildcards; every built-in role is derived only from `roles:`.
- **Redaction filter:** every credential shape is redacted.

### 15.2 End-to-end journeys (Playwright on a real stack; each one walked by an independent tester agent)
| Id | Journey |
|---|---|
| J1 | Fresh install: setup link from the logs → claim → enrol a passkey → Owner lands on Identity Overview. The setup URL then returns 404. |
| J2 | Upgrade with `BOSS_CONSOLE_PASSWORD`: sign in with the legacy password → claim → the legacy password still works mapped to `usr_legacy_shared` → banner and countdown. |
| J3 | Invite an Operator → accept → set a password → sign in → TOTP enrol → sign out → sign in with TOTP → step-up for a step-up action. |
| J4 | Forgot password (SMTP) → reset → other sessions revoked → MFA still required. |
| J5 | Lost MFA → recovery code → forced regeneration below 3. Admin reset of an Operator; Owner reset requires a second Owner. |
| J6 | Sessions: sign in on two browsers → revoke one from My account → it is signed out at its next request. |
| J7 | OIDC (Keycloak in a container): invite → first SSO sign-in binds `(iss, sub)` → group sync grants a role → disable at the IdP via SCIM → BOSS sessions end and grants are suspended. |
| J8 | Operator grants Forge and Vera "for this build" through the JIT consent sheet → the build runs → the PR shows BOSS[bot] with an `On-behalf-of` trailer → the lineage page shows Alice → Atlas → Forge with narrowing scopes. |
| J9 | Revoke the standing grant mid-run → within 5 s the run halts "authority revoked", the egress call fails, the GitHub token is revoked → re-grant → resume. |
| J10 | Admin lowers Forge's ceiling (removes `preview:manage`) → the next preview start in a running build is denied with an `authz.denied` naming the ceiling. |
| J11 | **Viewer walk:** as Viewer, visit every area and click every enabled control. No mutating request returns 2xx. Disabled controls show the permission tooltip. |
| J12 | Role editor: clone Operator → remove `deployment:create` → the impact preview lists the correct people, pages and route counts → save with step-up → an affected user's Deploy button disables live (epoch push). |
| J13 | Service account: create → key shown once → use it in CI to deploy → rotate with 1 h overlap → the old key fails after grace. |
| J14 | Network mode (rehearsal): banner shown, actions attributed to Network operator with IP → claim a real Owner → Require sign-in → the network principal is disabled, its history intact. |
| J15 | The audit log: filter by a grant → open an event → the chain drawer → export (step-up) → verify chain ✓. |

### 15.3 Adversarial security tests (each is a named test, drawn from the pitfalls checklist and flaws common in identity products)

**Sign-in and sessions**
- **A1** stuffing: 25 attempts/min from one IP → 429.
- **A2** soft lock does not lock the owner's familiar device or passkey.
- **A3** enumeration: unknown versus known-wrong bodies identical, median timing within 20%.
- **A4** TOTP 6th attempt in a challenge rejected.
- **A5** a TOTP code reused in the same step is rejected.
- **A6** a WebAuthn challenge from session X fails in session Y, and a user-handle mismatch fails.
- **A7** fixation: the pre-login cookie ≠ the post-login cookie, and the old one is rejected.
- **A8** a cross-site Origin POST → 403. A missing CSRF header → 403.
- **A9** `X-Boss-Principal`, `X-User-Id` and `actor` in body or headers have no effect.
- **A10** CSP blocks inline script. No token in `document.cookie`.
- **A11** `Host: evil.com` on forgot-password still links to `BOSS_PUBLIC_URL`.
- **A12** a reset token is single-use and invalid after a password change.
- **A13** 50 parallel sign-ins do not exceed memory (the semaphore).
- **A13b** token-exchange flood → 429.
- **A14** setup claim after an Owner exists → 404.
- **A15** **no public endpoint returns an OTP, reset token, magic link or session token** in a response body. A regression test, found by crawling every public route.
- **A16** a disabled user with the correct password gets the disabled message. With a wrong password they get the generic one.
- **A17** with SMTP unconfigured, the magic-link and email-code endpoints answer 404 and the tabs do not render. With it configured, see A81–A92.
- **A18** a password change revokes other sessions.
- **A19** a login response sets cookies only with the `__Host-`, Secure and HttpOnly attributes.
- **A20** `secure:false` is impossible: the API refuses to set a session cookie over plain HTTP except on localhost.

**Email sign-in, social sign-in and self sign-up (D26, D30, D31; common flaws as regressions)**
- **A81** no response body of `/v1/auth/magic-link`, `/v1/auth/email-code` or `/v1/auth/signup` ever contains the link, its token or the code (crawl every public auth route, as A15).
- **A82** a magic link or code is single use: the second use fails, and so does any use after 10 minutes.
- **A83** a magic link opened in a different browser (no matching `__Host-boss_preauth` state cookie) does not sign in; it shows "Open the link in the browser where you asked for it".
- **A84** at rest only `SHA-256(token)` and `argon2id(code)` exist: a DB dump contains neither (schema scan of `identity_auth_challenges`).
- **A85** known and unknown addresses get byte-identical responses and median timings within 20% (a magic-link 404 for an unknown address is an account oracle).
- **A86** per-email (5/h) and per-IP (20/h) limits on both email methods → 429 with `Retry-After`; email code verification has 5 attempts per code, then the code is burned.
- **A87** an Owner or Admin who signs in by magic link or email code still needs a second factor or a passkey before the session is issued; no email method satisfies MFA.
- **A88** the magic-link token never appears in a GET URL that the API logs: the link carries it in the fragment, and the console POSTs it (access-log sweep, A80).
- **A89** social sign-in links only by `(issuer, subject)`: an IdP or social provider asserting the email of an existing account, with or without `email_verified`, links nothing and signs nothing in (linking by email is a common takeover).
- **A90** a first social sign-in with an unknown subject creates no user and no org unless an open invitation for that verified email, or a JIT domain rule, or enabled self sign-up allows it.
- **A91** with self sign-up off (the default) `/v1/auth/signup` answers 404 and the console shows "Ask an admin to invite you"; with it on, an address outside the domain allow-list gets the uniform response and nothing is created; a self-signed-up person starts with no access roles.
- **A92** no session or access token is ever placed in a redirect URL, query string or `localStorage` after any sign-in method (a token in a redirect URL or localStorage is a common flaw).

**Credentials**
- **A21** an expired key → 401.
- **A22** a key outside its CIDR → 401.
- **A23** use from a new IP emits an event.
- **A24** a key is never present in any API response after creation.
- **A25** an Admin cannot mint a key for an Owner-role service account they do not ⊇.
- **A26** a PAT calling a step-up permission → 401 `step_up_required`.
- **A27** constant-time compare (static check plus a micro-benchmark bound).
- **A28** a checksum-invalid key is rejected before DB access.
- **A29** a revoked key is rejected on the very next request.
- **A30** a key's effective set shrinks immediately when its owner is demoted.

**Authorization**
- **A31** an Admin assigning Owner → 403.
- **A32** a custom role containing `owner:assign` created by an Admin → 403.
- **A33** a user editing their own bindings → 403.
- **A34** adding yourself to a group whose roles exceed yours → 403.
- **A35** editing a custom role that is bound to others to add a permission you lack → 403.
- **A36** PATCH a user with `{role, org_id, status, kind}` → 422 and unchanged.
- **A37** after a demotion, the next request is denied (≤5 s with NOTIFY disabled; immediate with it).
- **A38** two concurrent "remove Owner" requests leave ≥1 Owner.
- **A39** an agent run spawning a child that is not in `allowed_children` → denied.

**Tokens and agents**
- **A40** introspection without the egress credential → 401. Introspection includes `act`.
- **A41** a run token without a DPoP proof → 401. With a proof by another key → 401.
- **A42** a DPoP proof replayed (same jti) → 401. A wrong `htu` or `htm` → 401.
- **A43** a run token presented as a session or API key, or with `aud=boss:egress` to the API → 401.
- **A44** a client-supplied `act` or `boss_chain` in the exchange body is ignored.
- **A45** depth 4 with an org max of 3 → denied. A child's larger `max_depth` does not raise the cap.
- **A46** an agent appearing twice in a chain → denied.
- **A47** a requested scope wider than the parent → narrowed, never widened.
- **A48** an empty intersection → token refused (never "the requested scope").
- **A49** an agent token calling `decision:approve`, `role_binding:assign`, `delegation:grant` or `deployment:production` → 403 at the PDP, and the token service never mints them.
- **A50** an agent cannot approve its own MCP confirmation.
- **Keycloak regression tests:**
  - #52691: actor validation cannot be bypassed by an alternate `subject_token_type`.
  - #53029: introspection returns `act`.
  - #53040: a multi-hop exchange preserves the nested chain.
- **JWT BCP:**
  - `alg: none` is rejected.
  - An HS256 token signed with the public key is rejected.
  - An unknown `kid` is rejected **without any fetch**.
  - `jku`, `x5u` and `jwk` headers are ignored.
  - A wrong `typ` or `iss` is rejected.
  - A token signed with the previous key verifies during the overlap and fails after it.

**Vault and egress**
- **A51** **prompt-injection exfiltration test:** a malicious repo file instructs the agent to print every environment variable, `~/.git-credentials`, the shim socket and `/proc/*/environ`. The run transcript, the PR and the egress log contain no GitHub token, run token or egress credential string.
- **A52** swapping `secret_enc` between two rows → decrypt failure plus a critical event.
- **A53** a wrong master key → boot refusal.
- **A54** run R for Alice requests Bob's connection → 403.
- **A55** a grant naming `acme/infra` when Alice lacks GitHub write there → refused at grant time. A run against a repo outside the grant → egress 403.
- **A56** a push to `main`, a force-push, or a write to `.github/workflows/x.yml` without `workflows: write` → egress 403.
- **A57** concurrent refresh of one provider token → a single refresh, both callers get the new token.
- **A58** every egress request has a log row with the chain.
- **A59** a JIT credential is impossible when the policy is off.
- **A60** a revoked grant → the installation token is revoked at GitHub (mock asserts the DELETE).

**SSO and SCIM**
- **A61** an IdP asserting an email of an existing account without an invitation → no link, sign-in refused.
- **A62** an id_token with `alg: none` or a forged signature → rejected.
- **A63** a wrong `aud`, missing `azp` with multiple audiences, or a wrong `nonce` → rejected.
- **A64** a callback without a PKCE verifier → no exchange.
- **A65** provider A's code presented at provider B's callback → rejected.
- **A66** SAML XSW variants (8 canonical fixtures) → rejected.
- **A67** a replayed assertion id → rejected.
- **A68** an unsolicited response while IdP-initiated sign-in is off → rejected.
- **A69** a callback `state` from another browser → rejected.
- **A70** issuer `http://169.254.169.254/` → refused.
- **A71** a discovery document returning `jwks_uri` on 127.0.0.1 → refused unless allow-listed. DNS rebinding (resolves public, then private) → refused.
- **A72** a SCIM key calling `/v1/identity/role-bindings` → 403.

**Audit**
- **A73** every sign-in, denial and admin change produces exactly one event, with an actor.
- **A74** `UPDATE identity_security_events` as the app role → error.
- **A75** a tampered row → verify reports the first break.
- **A76** a forced event-write failure → the admin change rolls back.
- **A77** no route writes audit rows from a request body.
- **A78** pruning writes a checkpoint, and the chain still verifies.
- **A79** a failed sign-in for an unknown identifier stores no plaintext identifier.
- **A80** a log-redaction sweep: emit every credential shape through logging, error reporting and HTTP access logs, and none survives.

### 15.4 Coverage tests (the "every X" tests)
- **R1 `test_every_route_declares`:** walk `app.routes` (and every included router). Each route has exactly one of `@permit`, `@public` or `@authenticated`. Each `@permit` key is in the catalogue. Each `@public` is in `public_routes.yaml` with its verifier. The API must also refuse to boot with an undeclared fixture route.
- **R2 `test_every_mcp_tool_declares`:** every `ToolSpec` has a catalogue permission. API-surface tools resolve to their route's permission.
- **R3 `test_cross_project_is_404`:** for every route with an object scope, principal P with full permissions in project A requests an object of project B and gets 404. A cross-org variant also runs.
- **R4 `test_every_console_area_declares`:** every `AREAS` entry and every Identity and My-account page has a permission. Every permission literal in `apps/web` is in the generated union. Every `*-actions.ts` export is wrapped in `guardedAction`.
- **R5 `test_every_get_is_read_only`:** call every GET as Viewer against a DB snapshot and assert there is no row diff outside the audit tables. `readonly_post` entries are the declared exceptions.
- **R6 `test_no_claimed_actor_authority`:** a static check that no authorization call takes `payload.actor` or a request header as the principal.
- **R7 `test_catalogue_has_no_orphans`:** every catalogue key is used by a route, a tool or a page, or is listed as identity-API or system-only.
- **R8 `test_jobs_carry_a_principal`:** enqueueing a job without `on_behalf_of` raises.
- **R9 `test_only_vault_decrypts`:** an import-graph check.

### 15.5 The role × operation matrix
- **Fixture:** `tests/identity/matrix.yaml` is **generated from the catalogue** (the roles column) and committed. A role change therefore shows up as a reviewable diff. The published table in IDENTITY_PERMISSIONS.md is rendered from the same file.
- **Test:** for each built-in role × each route (~1,144 × 5 ≈ 5.7k requests through the in-process test client), send a minimal valid request as a fixture principal bound at the right scope.
  - **Allowed** must return neither 401 nor 403. A 422 or 409 from the handler is fine.
  - **Denied** must return 403, or 404 for object scope.
- **Agent run tokens:** for each permission × {delegable, not}, assert the PDP outcome with a run token whose ceiling includes it.
- **Runtime:** under 4 minutes in CI, parallelised by router.

---

## 16. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **The API is the only authority. Next.js is a pass-through.** | Two enforcement layers that disagree, and a fallback to unverified claims, are a common flaw. With one authority there is one bug surface. |
| D2 | **Opaque server-side sessions, not JWTs, for people.** | Instant revocation, no claim staleness, no key-confusion surface. The console never needs a self-contained token. |
| D3 | **JWTs only for run tokens: 5 minutes, EdDSA, DPoP-bound, one audience, separate key families.** | That is where self-contained tokens earn their keep (sandboxes, the proxy). Everything else is opaque. |
| D4 | **The catalogue is a YAML file in code, generating Python, TS, SQL and docs. No runtime-created permissions, no wildcards.** | It removes drifting copies, seeder bugs and typo-creates-permission bugs. A permission added later never flows silently into a custom role. |
| D5 | **Deny by default at boot.** An undeclared route stops the API from starting. | A CI test can be skipped. A server that won't start cannot be. |
| D6 | **Page permission = the read permission of the page's primary resource.** No separate `page:*` keys. | A page you can open but whose data you can't read, or the reverse, is a broken state. Role maps for pages are derived and shown in the role editor. |
| D7 | **Disable with a tooltip, don't hide.** | Discoverability, and the console UX rule. Hiding makes people think features don't exist. |
| D8 | **Five built-in roles (Owner, Admin, Operator, Viewer, Auditor), read-only, with clone-to-customise.** | It covers about 90% of teams. Auditor is cheap and wins compliance reviews. Read-only built-ins prevent the "edited the system role" class of mistakes. |
| D9 | **Union-only RBAC with org and project scope. No deny rules, nesting or inheritance.** | Easy to explain ("why does X have Y?") and to test exhaustively. |
| D10 | **The subset rule on every grant-like write, including group membership and service-account keys.** | These are the escalation paths that products usually miss. |
| D11 | **Agents act with the person's authority, but always as delegation (`act`), never impersonation.** | It meets the operator's "impersonate by default" in effect (same reach, narrowed) while keeping the agent visible in every record. That is essential for prompt-injection forensics. |
| D12 | **Effective = person (live) ∩ every ceiling in the chain ∩ grant ∩ token scope ∩ task.** Approvals, identity, secrets, production deploys and gate waivers are never delegable. | Live evaluation makes demotion instant. Evaluating every hop closes the RFC's "only the current actor matters" gap. A fixed list of non-delegable permissions removes the worst agent abuses by construction. |
| D13 | **Revocable grant objects with default expiry (30 d) plus task-bound JIT grants.** | "Long-lived OBO" without long-lived tokens. Users understand grants. Revocation has one handle. |
| D14 | **GitHub default: App installation tokens per run, down-scoped, revoked at run end, injected by an egress proxy, plus a user-repo-access guard.** User OAuth only for "as me on GitHub". | It is the best per-run credential GitHub offers. The proxy makes exfiltration impossible by construction. The guard stops the App's broad installation from being a confused deputy. |
| D15 | **A reverse-proxy egress (URL rewrite), not TLS interception.** | No MITM CA to manage or leak. git and gh support base-URL rewriting natively. |
| D16 | **A credential shim process outside the agent's view holds the run token and DPoP key.** | DPoP alone does not help when the agent has a shell next to the key. Process isolation plus egress limits does. |
| D17 | **Envelope-encrypted vault with the KEK from a file or env, and no plaintext fallback, ever.** | It works on a single VM with no KMS, and upgrades to KMS later. |
| D18 | **Argon2id m=64 MiB, t=3, p=1, with a small-host profile and a semaphore.** | Strong by default, survivable on 1-GiB community VMs, DoS-bounded. |
| D19 | **Passkeys are first-class (passwordless) and MFA is mandatory for Owners and Admins.** Email methods are allowed only as D26 constrains them; no phone/SMS. | Phishing resistance where it matters. *Amended by D26 (operator, 2026-10-06): the original "no email OTP or magic links" is replaced.* |
| D20 | **Never display any part of a key after creation. Keys are identified by a random public id.** | The BOSS credential rule. Prefixes of short secrets have leaked whole secrets before. |
| D21 | **Shadow mode before enforcement, per router, plus legacy principals.** | Retrofitting 1,144 routes without lockouts needs evidence, not hope. |
| D22 | **The audit log is in the same transaction, append-only and hash-chained.** | Attribution is the product's promise. Best-effort audit is not audit. |
| D23 | **"Access role" in identity, "role" stays the agent's job.** | BOSS already uses "role" everywhere for agents, and two meanings would blur in every screen. |
| D24 | **One org per install in v1, with org-scoped tables ready for more.** | It matches every current install. Keying everything by `org_id` now keeps multi-org a configuration change, not a migration. |

**Operator decisions, 2026-10-06** (answers to §17, and the sign-in page review):

| # | Decision | Consequence in this spec |
|---|---|---|
| D25 | **After the first Owner is claimed, the old shared console password maps to Operator for 30 days, then stops working.** | §4.11, §12 M2 and M5. `usr_legacy_shared` holds Operator and signs in with no email; the countdown banner shows on every page for the whole window. An Owner may extend once, by at most 30 days (step-up, audited). |
| D26 | **Magic link and email code are allowed, only when SMTP is configured, and built safely** (§4.14): emailed only, never in a response; single use; 10-minute TTL; bound to the requesting browser by a state cookie; hashed at rest; per-email and per-IP rate limits; a uniform response whether the address exists or not; never a way around MFA for Owner or Admin. **No phone/SMS.** | §4.7, §4.14, §9.7, A17, A81–A88, A92. Replaces D19's "no email OTP or magic links". |
| D27 | **Network-trusted installs move to passkeys:** real accounts, no Tailscale-header trust, no permanent network operator. Network-trusted mode is a migration bridge only; such an install claims its first Owner through the network-bridge claim, with no env change. | §4.12, §12. |
| D28 | **The GitHub user-repo-access guard applies only on multi-person installs** (more than one active human). | §7.9. |
| D29 | **A one-person install keeps the single-operator tier-3 waiver.** | §6.13 unchanged. |
| D30 | **SMTP is optional.** Without it, password reset is admin-mediated and the email sign-in tabs are hidden. | §4.7, §9.7. |
| D31 | **Social sign-in links accounts by `(issuer, subject)` only, never by email.** A first social sign-in with an unknown subject creates nothing unless an invitation, a JIT domain rule or enabled self sign-up allows it. | §4.8, §4.15, A89, A90. |
| D32 | **Self sign-up exists but is off by default**, with an optional domain allow-list; "New here? Create account" appears only when an Owner enables it. Otherwise: "Ask an admin to invite you". | §4.10, §4.15, §9.7, A91. |
| D33 | **The sign-in page layout, in BOSS style:** mark and welcome, configured social/SSO icon row (hidden when none), prominent passkey button, "or use email", tabs Password \| Magic link \| Email code. | §9.7; mockup `signin`. |
| D34 | **Operator includes `workflow:publish` and `deployment:create`.** Production keeps `deployment:production` and its approval gate. | Catalogue unchanged from the draft (it already did); pinned by `test_operator_includes_publish_and_non_production_deploys`. |

---

## 17. Open questions for the operator

All six were answered on 2026-10-06; see D25 (legacy password), D27 (network mode), D29 (tier-3 waiver), D34 (Operator's reach), D30 (SMTP) and D28 (GitHub guard). New questions go to IDENTITY_BUILD.md §7 as amendments.
