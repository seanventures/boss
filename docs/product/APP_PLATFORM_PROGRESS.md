# App platform build: progress

Spec: [APP_TEMPLATES_AND_SECURITY_LOOP.md](APP_TEMPLATES_AND_SECURITY_LOOP.md) (§8 is the build plan).
Branch: `feat/app-platform` (worktree `~/src/boss-wt-appplat`). Push after every step.

## Stream S0: day-1 interfaces

| # | Step | State |
|---|---|---|
| 1 | `db/schema.sql`: `-- BEGIN Fn … -- END Fn` blocks for F2, F4, F5, F6 with the tables the spec defines | done |
| 2 | Permission catalogue: the §8.2 permissions + regenerated files | done |
| 3 | Router stubs (`routes_templates.py`, `routes_security.py`, `routes_links.py`, `oidc_routes.py`), 501 `not_implemented_yet`, included in `main.py` | done |
| 4 | `engine/boss_engine/templates/manifest.py` (boss.template/v1, frozen) + unit tests | done |
| 5 | IDENTITY.md amendment for D10 (RS256 ID tokens for apps; EdDSA-only is BOSS-internal) | done |

### Notes per step

1. Schema. Every table §2.7, §2.4, §4.4, §5.2 and §6.3 lists column by column is created inside its
   feature's block (types inferred from the spec: ids TEXT, closed sets as CHECKs). Not specified
   column by column, left as `TODO(S6)` in the F6 block: `oidc_pool_user_factors`, `oidc_sessions`.
   `app_secret_bindings.mode`'s CHECK is table-level because the column reconcile in
   `boss_engine/db.py` cuts an inline definition at the word "generated". `ensure_schema` applied
   twice on a fresh database: 961/961 statements, no new reconcile skips.

2. Catalogue (`packages/identity/catalogue.yaml`, regenerated with
   `PYTHONPATH=engine python3 -m boss_engine.identity.codegen`). 180 → 192 permissions; the reviewed
   role sizes are updated in `engine/tests/test_identity_catalogue_is_generated.py` and
   `apps/web/tests/identity-contract.test.ts`.

   | Permission | Scope | Roles | Flags |
   |---|---|---|---|
   | `template:read` | org | O A P V U | delegable |
   | `template:import` | org | O A P | — |
   | `template:approve` | org | O A (D3) | step-up, critical |
   | `app_secret:read_metadata` | project | O A P | delegable |
   | `app_secret:write` | project | O A P | step-up (D9: reveal test users is behind it) |
   | `app_secret:live` | project | O A | step-up, critical |
   | `security_scan:run` | project | O A P | delegable |
   | `security_finding:triage` | project | O A P | — (a person's judgment, not delegable) |
   | `oidc:admin` | org | O A | step-up, critical |
   | `oidc_client:manage` | project | O A P | step-up |
   | `oidc_pool_user:manage` | org | O A | step-up |
   | `oidc_app:sign_in` | project | O A P V | — (bound at a project = may sign in to its apps) |

   Not done (owner streams decide): no Passport preset holds a new permission yet. S2 decides whether
   the builder/planner presets get `template:read`; S4 whether the reviewer preset gets
   `security_scan:run`.

   The spec's `app_secret:read_meta` is named `app_secret:read_metadata` (the catalogue's read
   actions are `read`, `read_metadata`, `read_all`; the spec now says so too).

3. Router stubs. `apps/api/app/routes_templates.py` (S2), `routes_security.py` (S4),
   `routes_links.py` (S5), `oidc_routes.py` (S6), included at the end of `main.py` before the
   coverage check. 54 operations, all declared (coverage 1442/1442). Each answers 501
   `{"error": "not_implemented_yet", "owner": "<stream>", "spec": ...}`;
   `apps/api/tests/test_app_platform_day1_routes.py` pins that under enforce (owner reaches every
   stub; no credential reaches only `/oidc/*`; a viewer cannot import or approve). A stream that
   builds its routes updates that test.
   Decisions made here (S-owners may revisit):
   - App-level routes take `project_id` (query for GET/DELETE, body otherwise): an app key is
     unique per project only.
   - Link create/revoke use `project:update` (the spec's "project:write" does not exist). That is
     Owner/Admin + step-up: operators cannot link apps until S5 or the operator decides otherwise.
   - D9's reveal is `POST /v1/apps/{app_key}/test-users/reveal` (`app_secret:write`, step-up).
   - OIDC admin GETs are declared with the manage keys (no `oidc:read` exists; listed in
     `GET_NOT_READ` of `test_identity_authz_read_only.py`). Open question for S6: does the Auditor
     need an `oidc:read`?
   - Protocol routes: discovery and JWKS are `public:jwks`; authorize, token, userinfo,
     revocation and end_session are `public:oidc_provider_endpoint`, a new `PublicReason`
     (IDENTITY_BUILD.md §7, AP-2) with verifier `oidc_routes.verify_oidc_request` (S6).
     `/oidc/` joins SCIM and SAML as a foreign protocol outside the idempotency contract.
   - New object kinds (`identity/object_kinds.py`): `security_scan`, `security_finding` (project via
     its scan), `app_link`, `oidc_client` (so `oidc_clients.project_id`/`app_key` are NOT NULL: a
     client belongs to one app).
   - `app_secret:live` and `oidc_app:sign_in` are checked inside handlers, not by a route: listed
     in `RESERVED` of `test_identity_authz_catalogue_use.py`.
   Regenerate after any route change:
   `PYTHONPATH=.:engine python3 -m apps.api.tests.identity_matrix`,
   `PYTHONPATH=.:engine python3 apps/api/tests/test_identity_authz_public_routes.py`,
   `PYTHONPATH=.:engine python3 scripts/dump-identity-api-routes.py`.
   Known failure on the base, not from this work:
   `test_a_streaming_write_is_excluded_rather_than_buffered` (`POST /v1/agents/{role_key}/test`).

4. Manifest. `engine/boss_engine/templates/manifest.py`: `validate_manifest(text|dict)`,
   `load_manifest(path|dir)`, `TemplateManifest` (pydantic, frozen, `extra="forbid"`),
   `ManifestError.reasons` (one `<field>: <what is wrong>` line per problem, all at once).
   Checks the manifest alone: id/semver/word/env-name patterns, stacks ∈ node|python|java, a test
   command (junit, min_tests ≥ 1) for exactly the declared stacks, compilable secret patterns,
   egress = bare FQDNs (no wildcard, IP, scheme/port, local/reserved TLD), `METHOD /path` routes,
   relative guide/security paths, no self-reference, no duplicate names. The import's checks that
   need the repo or the install (tree, sizes, secret scan, built-in id clash, BOSS hosts in egress,
   who may set `insecure_by_design`) are S2's. Tests: `engine/tests/test_template_manifest.py`
   (45; the spec's example verbatim is valid; the field set is pinned).

5. D10. IDENTITY.md gains D35 and a paragraph under the token families: "EdDSA only" is for
   BOSS's own tokens; app ID tokens are RS256 by default, EdDSA per client, in a separate `oidc`
   key family neither side accepts from the other; D3 points at D35. The amendment log
   (docs/maintainers/IDENTITY_BUILD.md §7) records AP-1 (catalogue), AP-2 (`oidc_provider_endpoint`)
   and AP-3 (D35).

## What's next (after S0)

S0 is complete. Per §8.2 the next streams start in parallel against these interfaces: S2 (F2
engine/API: `engine/boss_engine/templates/` beyond `manifest.py`, `routes_templates.py`, the
`template_tests` gate, vault kind `app_secret`, which needs `identity_vault_items.kind`'s CHECK
widened in the identity block by amendment), S4, S5, S6, then the UI streams. Each stream edits only
its own schema block and router module, and updates `test_app_platform_day1_routes.py` as its
routes stop answering 501.

## Resume instructions

- `cd ~/src/boss-wt-appplat && git pull` (branch `feat/app-platform`).
- Pick up the first step above that is not `done`.
- Test DB: container `boss-test-pg` on localhost:55432, user `boss`; password via
  `docker exec boss-test-pg printenv POSTGRES_PASSWORD` (never print it). Create a private database
  (S0 used `boss_test_s0`) and drop it afterwards.

## Stream S6: BOSS as an OIDC provider (F6)

Worktree `~/src/boss-wt-s6-oidc`, branch `feat/s6-oidc` (from `feat/app-platform`
@ 8b7cc39d). Engine: `engine/boss_engine/oidc_provider/`; routes: `apps/api/app/oidc_routes.py`.

| # | Step | State |
|---|---|---|
| 1 | Protocol core (discovery, JWKS, authorize, consent, token, userinfo, revocation, end_session), client registry API, workforce pools on the console session | done |
| 2 | Unit, admin API and adversarial suites (the named list) | done |
| 3 | End-to-end proof: a Node app with openid-client on a local stack | done |
| 4 | OpenID conformance suite (Basic OP), if Docker allows | not run (see below) |
| P2.1 | Customer pools: hosted sign-in/sign-up/email code/TOTP/passkeys/reset/account pages, pool-user API, web proxy | done |
| P2.2 | Re-authentication for prompt=login/max_age, step-up for require_mfa | done |
| P2.3 | Console Identity ▸ App sign-in | done (independent tester walk: todo) |
| P2.4 | Conformance (if a stack slot is free) | blocked by decision (see below) |

### Decisions and notes

- **Issuer** = `${BOSS_PUBLIC_URL origin}/oidc/<org_id>` (the org slug is the org id: `orgs` has no
  slug). Built only from the configured public origin, never from Host; no public origin = 503.
- **Keys**: the `oidc` family lives in its own table `oidc_signing_keys` (per org, RS256 + EdDSA,
  next/active/retiring/retired, private halves in the vault). BOSS-internal verifiers only read
  `identity_signing_keys`, and the oidc verifier only reads `oidc_signing_keys` (kids start
  `oidc-`): the separation is structural. `signing_keys.rotate_oidc_family_if_due` is the one
  function S6 adds there; keys are provisioned when the first pool or client is created, and
  `keys.signing_key` rotates a due key on use. TODO (identity owner): call
  `rotate_oidc_family_if_due` from `identity/maintenance.py` next to `rotate_if_due`.
- **Grants**: one `oidc_grants` row per sign-in; the code, every access token (`sid` claim) and the
  refresh family hang off it. Code replay, refresh reuse, a code or refresh token presented by
  another client, logout with `id_token_hint`, consent revoke, client disable/delete and losing
  `oidc_app:sign_in` all revoke the grant; userinfo checks it live.
- **Interactions** (`oidc_interactions`): no session -> `/login?next=/oidc/<org>/resume?interaction=…`
  (single use, 10 min); consent page -> POST `/oidc/<org>/consent` (session + Origin + HMAC(session,
  interaction) token). Two routes beyond S0's stubs: `GET /oidc/{org}/resume`, `POST /oidc/{org}/consent`.
- **Refresh tokens** only with `offline_access`, which always needs an explicit consent (even
  `first_party`). Family max age 90 days, each token 30 days, rotated on every use.
- **Access tokens**: RS256 always, `typ: at+jwt`, `aud` = client `resource` or client_id.
- **Pairwise `sub`** = HMAC(pool subject key, subject ‖ client); `public` = HMAC(pool key, subject).
  Never a BOSS id. The pool key is random, in the vault.
- Phase-1 limits (prompt=login/max_age answered `login_required`, require_mfa as an error page,
  customer pools unavailable) are lifted by phase 2 (below).
- **Amendment AP-4** (IDENTITY_BUILD.md §7): `oidc.*` audit events, `oidc.*` rate-limit buckets,
  the one signing_keys function.
- Open question from S0 (Auditor `oidc:read`): not added; every admin GET needs `oidc:admin` or
  `oidc_client:manage` (both step-up). Revisit with the console UI in phase 2.

### Tests (S6)

- `engine/tests/oidc/test_oidc_rules.py` (44, no DB): S256 vector, redirect-URI rules, client
  validation, inline-JWKS rules, page escaping and headers.
- `apps/api/tests/test_oidc_provider_flow.py` (10): discovery, JWKS, the whole workforce flow,
  sign-in redirect and resume, remembered consent, EdDSA client, public client, revocation.
- `apps/api/tests/test_oidc_provider_admin.py` (11): permissions, secret once, first-party, no URL
  fields (422), roles in tokens, consent revoke and client delete end tokens.
- `apps/api/tests/test_oidc_provider_adversarial.py` (99): A1 code replay, A2 PKCE, A3 redirect_uri
  tricks (16 shapes at authorize, 12 at registration), A4 mix-up, A5 refresh reuse, A6 alg/typ/
  key-family confusion (none, HS256 with the public key, rogue key under a real kid, ES256, BOSS
  run/actor typ, ID token as AT, AT as id_token_hint, app tokens and refresh token as BOSS bearer,
  console cookie as AT), A7 aud/azp and private_key_jwt (8 bad assertions, jti replay), A8 nonce,
  A9 pairwise/public subjects and client binding, A10 app cookie on a BOSS route, A11 CSRF on
  consent (Origin, Sec-Fetch-Site, token, other session, reuse) and prompt=none, A12 leakage (logs,
  audit details, hashed at rest, no AT in URLs), A13 client auth downgrade and lockout, A14 grant
  loss, disabled person/client, legacy shared session, require_mfa, A15 logout open redirect, A16
  request/request_uri/registration/response modes/duplicates.

### End-to-end proof (S6)

`tests/e2e/oidc/` (`run.sh <console origin>`; `setup_world.py` + `rp.mjs`, openid-client 6.8.1
pinned). Run 2026-10-08 on a local stack: API (`python -m apps.api.serve`, port 18093, DB
`boss_test_s6`, BOSS_PUBLIC_URL=http://localhost:13000) + console (`next dev`, port 13000,
forwarding /oidc/* through `apps/web/lib/oidc-proxy.ts`). A compose stack was not used: bosss4
then bosss2 held the one-stack slot. Result, all through the console origin: discovery -> consent
page for a signed-in member -> consent POST -> callback with `iss` -> openid-client code exchange
(state, iss, PKCE, nonce, ID token signature) -> userinfo (name, email, roles) -> refresh rotation
-> userinfo with the refreshed AT -> end_session back to the app -> the AT is dead (401) -> the
rotated refresh token is refused. `E2E PASS`. Also checked by hand: no session -> 302 to the
console `/login?next=/oidc/<org>/resume?...` (200), an unregistered redirect_uri -> 400, no Location.
Found and fixed by the e2e: a token-endpoint 401 for a non-Basic client carried a Bearer challenge
(openid-client v6 treats any challenge as fatal); it now carries none (Basic when Basic was used).

### Phase 2.1: customer pools (S6)

- `oidc_provider/customers.py` is the realm: pool users (`opu_`, no identity_principals row), sessions
  (`oidc_sessions`, cookie `__Host-boss_app_session`, `bas_` tokens, 12 h idle / 7 d absolute, a NEW
  session at every sign-in and MFA), challenges (`oidc_app_challenges`, new table, bound to
  `__Host-boss_app_preauth`, 5 attempts, 10 min), Argon2id + the fair hash queue + new-password rules
  (passwords.py), TOTP via totp.py's constant-time ±1-step matching with last-step replay
  prevention (secret in the vault), passkeys via py_webauthn with identity's `rp_for` (RP ID = the
  BOSS host, user handle = pool user id, credential looked up in THIS pool only), emailed 6-digit
  codes (email_signin.DELIVER) for verification and reset, rate limits and soft lock in `oidc.*`
  buckets (AP-5). Every lookup names the pool.
- Deviation from §6.3: a `__Host-` cookie must have `Path=/`, so the app cookies are `Path=/`
  (host-only, Secure, HttpOnly, Lax). They are read only by the OIDC module; the console proxy
  forwards them only on /oidc/ browser steps; identity's authn never reads them.
- `oidc_provider/hosted.py`: pages and steps. `GET /oidc/{org}/account?view=signin|signup|forgot|
  account` and `POST /oidc/{org}/hosted/{step}` (signin, signup, verify-email, mfa, enrol-totp,
  forgot, reset, passkey-options, passkey-verify, account; wf-reauth for workforce). Steps check
  Origin + Sec-Fetch-Site; JSON steps need application/json; account actions need an HMAC of the app
  session. Pages: strict CSP with a nonce for the one passkey script, no framing.
- `protocol.py` is pool-agnostic now (`Who`): customer pools sign in on the hosted pages;
  `pool.mfa = required`, `client.require_mfa` or `acr_values=urn:boss:acr:mfa` force a second
  factor (enrolment if none). Signup: open, or domain; invite/closed = admin invite only (the
  invitee sets a password through "Forgot password"). `password_policy.verify_email` (default true)
  needs mail; `min_length` (default 12).
- Pool-user API (`oidc_pool_user:manage`): list, invite, PATCH status/reset_mfa/name; disable and
  MFA reset end sessions, challenges and every grant.
- Tests: `apps/api/tests/test_oidc_provider_customers.py` (26): the flow, then C1 a pool user is
  never a principal (no row; app cookie, app session as console cookie, as bearer, customer
  AT/ID token on `/v1/identity/me` and `/v1/projects` under enforce: 401; a workforce client never
  takes an app session; no BOSS or cross-client role), C2 cross-pool/org (session, challenge,
  same email in two pools, admin API from another org and on a workforce pool: 404, pairwise
  subjects), C3 anti-enumeration (same page for unknown email and wrong password; taken email at
  sign-up: same page, no mail, no verifiable code) and lockout, C4 TOTP replay and 5-strike
  challenge, password alone never finishes an MFA pool, C5 passkeys (register on the account page,
  sign in with the passkey alone; forged user handle, another pool, another origin, register
  without a session: refused), C6 CSRF on steps and account actions, framing/CSP, C7 consent revoke
  on the account page, disable, reset by code ends sessions, invite + MFA reset, viewer 403, C8 a
  new session at every sign-in.

### Phase 2.2: re-authentication (S6)

- Workforce: `prompt=login`, an exceeded `max_age`, or `require_mfa`/`acr_values=urn:boss:acr:mfa`
  with a session that has no second factor -> the hosted "Confirm it's you" page. The step is
  identity's own `signin.step_up` (TOTP with its replay guard; the password only for a member with
  no factor and only when MFA is not required) or `signin.passkey_options/verify` with purpose
  `step_up`; it rotates the console session (the new cookie is relayed) and the request continues
  with `since` = the interaction's creation, so only a confirmation made after the request counts.
  `prompt=none` with a stale session answers `login_required`/`interaction_required`.
- A workforce session counts as MFA only when it has a second-factor method (totp, passkey,
  webauthn, recovery_code, sso), not merely a `mfa_at` stamped by a password step-up: acr
  `urn:boss:acr:pwd` then.
- Customers: the same triggers show the hosted sign-in again; MFA-required asks the second factor
  (or enrolment).
- Tests: `apps/api/tests/test_oidc_provider_reauth.py` (R1 confirm with TOTP rotates the session
  and issues a fresh auth_time, R1 the same TOTP code cannot confirm twice, R2 max_age incl.
  prompt=none, R3 require_mfa refuses a password, R4 password route for a member with no factor
  gives acr pwd, R5 CSRF and no session).

### Phase 2.3: the console (S6)

- `apps/web/app/identity/app-sign-in/` (page + `app-sign-in.tsx`), registered in
  `lib/identity/pages.ts` (tab "App sign-in", access `anyScope: oidc_client:manage`), styles in
  `app/identity/identity-app-sign-in.css`. Tabs Pools · Clients · Users · Roles; rows open side
  sheets; register shows issuer, client id and secret ONCE in a locked sheet; rotate secret;
  disable/remove; pool create/edit/delete; invite, disable/enable, reset MFA; roles add/assign/
  remove. The admin reads are step-up permissions, so the lists load through `useGuardedCall`
  (the step-up sheet opens on first load). Recommendation (needs a catalogue amendment): an
  `oidc:read` (Auditor, Admin, Owner; no step-up) for the lists.
- Proxy: `lib/oidc-proxy.ts` forwards `account` and `hosted/<step>` as browser steps (console
  session + app cookies + Origin/Sec-Fetch-Site) and relays only the identity Set-Cookies.
- Tests: `apps/web/tests/identity-app-sign-in.test.tsx` (6), `oidc-proxy.test.ts` (5); all 35
  identity web test files pass; tsc clean. Not yet done: an independent tester's browser walk.

### Phase 2.4: conformance (S6) — not run, needs an operator decision

The slot was free (2026-10-09) and the suite has prebuilt images (`docker-compose-prebuilt.yml`),
but the OpenID **Basic OP** plan (`oidcc-basic-certification-test-plan`, code flow,
client_secret_basic) sends no PKCE in every test but one (`oidcc-ensure-request-with-valid-pkce-
succeeds`) and requires a code flow WITHOUT a nonce to succeed (`OP-nonce-NoReq-code`); it also
requests `address` and `phone` scopes. §6.2 makes PKCE S256 mandatory for every client and nonce
mandatory for `openid`, and BOSS refuses unknown scopes; so nearly every module would fail at
authorize by design. Running it is only meaningful after one of: (a) a per-client "OIDC
certification compatibility" switch (PKCE optional for confidential clients, nonce optional, unknown
scopes ignored), off by default and Admin-only; or (b) accepting that BOSS is not Basic-OP
certifiable and running the suite's individual modules that match the profile (discovery, ID token
signature/kid, userinfo GET/POST header/body, code reuse, redirect_uri not registered, valid PKCE,
end_session). Decision for the operator (suggested D14).

### Resume (S6)

- `cd ~/src/boss-wt-s6-oidc && git pull` (branch `feat/s6-oidc`).
- Tests: DB `boss_test_s6` on boss-test-pg (localhost:55432, user boss; password via
  `docker exec boss-test-pg printenv POSTGRES_PASSWORD`, never printed). Run with
  `DATABASE_URL=postgresql+psycopg://boss:$PW@localhost:55432/boss_test_s6 PYTHONPATH=.:engine python3 -m pytest -q apps/api/tests/test_oidc_provider_*.py`.
- DB `boss_test_s6` was DROPPED at the end of phase 1: `CREATE DATABASE boss_test_s6` first.
- Conformance (step 4), not run: the suite is its own compose stack (server, MongoDB, nginx), and
  the one-stack slot was held (bosss4, then bosss2). It also needs the issuer reachable from its
  containers (BOSS_PUBLIC_URL on host.docker.internal, or the suite on the host network) and a
  browser step for authorize (its `browser` config: a cookie of a signed-in member, the consent
  click). Expected gaps it will flag: `prompt=login` and `max_age` re-authentication
  (`login_required` today), and `response_mode=form_post` (not offered).
- Phase 2, in order: (1) customer pools: `oidc_pool_users`, `oidc_sessions` (cookie
  `__Host-boss_app_session`, Path=/oidc/), `oidc_pool_user_factors` (tables exist), reusing
  passwords/totp/webauthn/ratelimit/email_signin parameterised by a realm; sign-in/sign-up/MFA/
  passkey hosted pages; the pool-user admin routes (still 501); (2) re-authentication for
  `prompt=login`/`max_age` and step-up for `require_mfa` instead of the error page; (3) the
  console Identity ▸ App sign-in (Pools, Clients, Users, Roles; side sheets); (4) hosted pages in
  the console's sign-in style; (5) conformance.
- After a route change regenerate: `python3 -m apps.api.tests.identity_matrix`,
  `python3 apps/api/tests/test_identity_authz_public_routes.py`, `python3 scripts/dump-identity-api-routes.py`.
