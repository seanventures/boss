# BOSS Identity: build plan and stream ownership

Status: **Phase 0 complete (frozen), 2026-10-06**, on branch `id/identity` from main @ b1befb7d.
Spec: [IDENTITY.md](IDENTITY.md) (decisions D1–D34). Catalogue: `packages/identity/catalogue.yaml`, published as [IDENTITY_CATALOGUE.md](IDENTITY_CATALOGUE.md). Draft mappings: [IDENTITY_PERMISSIONS.md](IDENTITY_PERMISSIONS.md) §5–§8.

Five streams build in parallel against the interfaces Phase 0 froze. **No two streams edit the same file** (the one exception, `apps/api/app/main.py`, is split into named zones in §5). A change to anything in §2 is an **amendment** (§7): written down, agreed by the lead, then made by the lead, never by a stream on its own.

---

## 1. Where Phase 0 left things

| Measure | Now | Target |
|---|---|---|
| Permissions in the catalogue | 180 (Owner 175, Admin 166, Operator 83, Viewer 46, Auditor 59; 5 system-only) | frozen |
| API operations (Community edition app, incl. WebSockets and framework routes) | **1,164** | — |
| Declared | **2** (`GET /v1/identity/me`, `GET /v1/identity/me/permissions`, both `authenticated`) | 1,164 |
| Undeclared (in `tests/identity/undeclared_routes.txt`) | **1,162** | 0 |
| MCP / engine tools | 114 hand-written, **0 declared**; the API-surface tools inherit their route's declaration and the two discovery tools are `system:read` | 114 |
| Console areas | 35, **0 declared** (`tests/identity/undeclared_console_areas.txt`) | 35 |
| Enforcement mode | `BOSS_AUTHZ_MODE=shadow` (default), coverage `warn` | `enforce`, coverage `fail` |

Booting the API prints, once:
```
[identity] route coverage: 1164 operations, 2 declared (permit 0, public 0, authenticated 2), 1162 undeclared (coverage policy: warn)
[identity] shadow would deny GET /health: undeclared_route (undeclared)
[identity] enforce denied GET /v1/identity/me: not_evaluated (authn: identity.legacy.bridge is stream S5's)
```
Each would-deny prints once per route, reason and principal kind, then is only counted. The identity API is always enforced, and while authentication is unbuilt it answers `503 {"error":"identity_unavailable"}`. That is fail-closed, by design.

---

## 2. What is frozen

### 2.1 The catalogue
- **Source:** `packages/identity/catalogue.yaml`: permissions grouped by category, each with `roles` and `flags` (`step_up`, `delegable`, `critical`, `system_only`, `readonly_post`), plus the 8 access roles, the system principals and the 7 Passport presets.
- **Generator:** `PYTHONPATH=engine python -m boss_engine.identity.codegen` (`--check` exits 1 when stale). It validates every rule (key format, no wildcards, critical implies step-up and is never delegable or held by Viewer/Auditor, the never-delegable list, presets delegable-only, Owner holds every human permission) and writes:
  - `engine/boss_engine/identity/permissions_gen.py`: `Permission` StrEnum (`P` alias), `PERMISSION_META`, `ACCESS_ROLES`, `ROLE_PERMISSIONS`, `PASSPORT_PRESETS`, `SYSTEM_PRINCIPALS`, `CATALOGUE_FINGERPRINT`;
  - `apps/web/lib/identity/permissions.gen.ts`: the closed `Permission` union, `PERMISSION_META`, `ROLE_PERMISSIONS`, `isPermission`;
  - `db/identity_seed.sql`: upserts `identity_permissions` and the built-in `identity_access_roles` and resets their permission rows. `ensure_schema` applies it right after `db/schema.sql`;
  - `docs/product/IDENTITY_CATALOGUE.md`.
- **Tests:** `engine/tests/test_identity_catalogue_is_generated.py` checks that the outputs are current, pins the role sizes and rejects a broken YAML.

### 2.2 The schema
The `BOSS IDENTITY` block at the end of `db/schema.sql` is additive and reconciled by `ensure_schema`. It has 37 tables (plus the default egress partition), 3 trigger functions and 5 triggers. Its groups:
- **Principals:** `identity_principals`.
- **People and credentials:** `identity_users` (unique on `lower(email)`), `identity_user_identities` (links by `(issuer, subject)`), `identity_providers`, `identity_provider_group_mappings`, `identity_totp`, `identity_webauthn_credentials`, `identity_recovery_codes`, `identity_auth_challenges` (including `magic_link`, `email_code`, `self_signup_verify`).
- **Sessions:** `identity_sessions`, `identity_invitations`.
- **Groups:** `identity_groups`, `identity_group_members`.
- **Roles:** `identity_permissions`, `identity_access_roles`, `identity_access_role_permissions`, `identity_access_role_versions`, `identity_role_bindings`, `identity_approval_seats`.
- **Service accounts and keys:** `identity_service_accounts`, `identity_api_credentials` (hash only).
- **Agents:** `identity_agent_passports`, `identity_agent_ceilings`, `identity_delegation_grants`, `identity_delegation_grant_permissions`.
- **Run tokens:** `identity_run_credentials` (jti ledger), `identity_dpop_replay`, `identity_egress_credentials`, `identity_egress_log` (partitioned, with a default partition).
- **Vault and keys:** `identity_vault_keks` (KCV), `identity_vault_items` (AAD-bound ciphertext), `identity_signing_keys` (JWKS rotation).
- **Policy:** `identity_settings` (policy and epoch), `identity_rate_limits`.
- **Migration:** `identity_legacy_mappings`, `identity_authz_shadow`.
- **Audit:** `identity_security_events` (append-only, hash-chained).

The triggers:
- **Append-only audit:** UPDATE always raises. DELETE raises unless the audited prune sets `boss.identity_audit_prune = on`.
- **Delegable-only:** ceilings and grant permissions must be delegable.
- **Last Owner:** a deferred constraint trigger on bindings and principals. It fires only when Owner reach is removed, and only once a `bootstrap.claimed` event exists.

### 2.3 The Python interfaces (`engine/boss_engine/identity/`)

| Module | Frozen surface | Body owner |
|---|---|---|
| `principal.py` | `Principal`, `PrincipalKind`, `PrincipalStatus`, `AuthMethod`, `AgentInstance`, `ActorChain` (RFC 8693 `act`, current actor outermost; `to_act_claim`, `from_act_claim`, `to_boss_chain`, `has_cycle`), `SessionRef`, `CredentialRef`, `RunMode`, `TaskBinding`, `RunTokenClaims`, `AuthContext` | implemented |
| `catalogue.py`, `codegen.py`, `permissions_gen.py` | §2.1 | implemented |
| `scope.py` | `Scope.org/project/of`, `ScopeSpec`, `ResolvedScope` (`label()` = `org` or `project:<id>`), `resolve`, `register_object_kind`, `ScopeNotFound` (404), `ScopeUnresolved` | resolution implemented; **S2** registers object loaders |
| `declarations.py` | `@permit(permission, scope)`, `@public(reason, verifier)`, `@authenticated(composite, self_only)`, `PublicReason` (closed), `access_of` | implemented |
| `authn.py` | cookie, header and key-prefix names, `RequestCredentials`, `AuthnError`, `authenticate`, `parse_api_key`, `hash_token`, `check_csrf` | **S1** |
| `authz.py` | `Decision` (status, `error_body()`), `DenyReason`, `Why` (`role operator ← group Builders ← binding at project:x`), `EffectivePermissions`, `check` (= `authorize`), `effective_permissions`, `explain`, `bump_epoch` | **S2** |
| `delegation.py` | token constants (typ, aud, TTLs, depth caps, claim names, endpoints), `GrantRequest`, `DelegationGrant`, `TokenExchangeRequest`, `IssuedToken`, `CascadeTrigger`, `CascadeReport`, `create_grant`, `mint_run_token`, `exchange_token`, `verify_run_token`, `revoke_cascade(dry_run=)` | **S3** |
| `vault.py` | `aad()` (implemented: `item‖owner‖provider‖kind` with US separator), `VaultItemKind`, `CredentialPurpose`, `ConnectionRef`, `ShortLivedCredential`, `get_credential`, `store`, `revoke`, private `_decrypt` | **S3** |
| `audit.py` | `SecurityEvent` (`from_context`), `Stream`, `Outcome`, `EVENT_TYPES` (closed), `canonical_json` and `chain_hash` (implemented), `GENESIS_HASH`, `record`, `verify_chain`, `redact` | **S5** |
| `legacy.py` | reserved ids `usr_legacy_shared`, `sys_legacy_console`, `usr_network_operator`, `LegacyKind`, `bridge`, `note_claimed_actor` | **S5** |
| `policy.py` | `IdentityPolicy` (every key and default), `get_policy` | **S5** (others read it) |
| `http.py` | `fetch(method, url, purpose=)`, `OutboundRefused` | **S1** (S3 uses it) |
| `ratelimit.py` | `hit(bucket, identifier, limit, window_seconds)`, `BUCKETS` | **S1** (S3 uses it) |
| `ids.py` | `new_id(prefix)` | implemented |
| `contracts.py` | `MeResponse`, `MePermissionsResponse` and parts | frozen shapes |
| `enforcement.py` | `AuthzMode`, `mode_for`, `coverage_policy`, `evaluate` (never raises; a missing stream piece yields `NOT_EVALUATED`), `ShadowLedger` and `LEDGER`, `RouteEntry`, `CoverageReport`, `check_coverage`, `shadow_tool_call` | implemented |

Every stream-owned function raises `NotImplementedError("… is stream Sx's")`. `test_every_stream_owned_function_is_a_named_stub` lists them. A stream that implements one removes it from that list in the same change.

### 2.4 The enforcement point
- **The dependency:** `apps/api/app/identity_guard.py`. `enforce_access` is passed as `FastAPI(dependencies=[...])` in `main.py`, so it sits on **every** HTTP and WebSocket route, including included routers and edition re-mounts. `test_the_guard_is_on_every_route_of_the_real_app` checks this.
- **What it does per request:** it finds the matched route, reads the endpoint's declaration, builds `RequestCredentials`, then runs `evaluate` in the threadpool: authn (falling back to `legacy.bridge`), then scope resolution, then `authz.check`.
  - Shadow records would-denies.
  - Enforce raises `AccessRefused`, rendered as the spec's error body (`401 unauthenticated`, `401 step_up_required`, `403 {"error":"forbidden","permission":…}`, `404 not_found`, `503 identity_unavailable`). A WebSocket is closed with 1008.
- **Modes:**
  - `BOSS_AUTHZ_MODE=off|shadow|enforce`;
  - `BOSS_AUTHZ_ROUTER_MODES="setup=enforce,…"`, keyed by the endpoint's module tail (`main`, `setup`, `identity_me`, …);
  - router keys starting `identity` are always enforced unless the mode is `off`.
- **Boot check:** `identity_guard.check_app_coverage(app)` runs at the end of `main.py`. `BOSS_AUTHZ_COVERAGE=warn|fail` sets the policy; the default is `fail` under `enforce`. Under `fail`, any undeclared route raises `UndeclaredRoutesError` and the API does not start.
- **MCP:** `ToolSpec.permission` is validated against the catalogue at construction, and `ToolSpec.inherits_route_permission` is set on the API-surface tools. `ToolRegistry.invoke` calls `shadow_tool_call`, which S2 replaces with a real check.
- **Console:** `apps/web/lib/identity/areas.ts` holds `AREA_ACCESS`, which is `Partial<Record<AreaId, …>>` until S2 completes it.

### 2.5 The console contract (`apps/web/lib/identity/`)
- **`contract.ts`:**
  - `MeResponse` and `MePermissionsResponse`, mirrored field for field from `contracts.py`. `tests/identity-contract.test.ts` fails on drift.
  - `ME_PATH`, `ME_PERMISSIONS_PATH`, `CSRF_HEADER`.
  - `snapshotFrom(response)`, which drops unknown keys.
  - `can(snapshot, permission, scope)`, which returns `{allowed, reason, needsStepUp}`. It answers "cannot" for any scope it has no snapshot for.
- **`permissions.gen.ts`:** generated.
- **`areas.ts`:** S2's declaration table.

### 2.6 The shrinking lists
These files hold today's undeclared items and may only shrink: `tests/identity/undeclared_routes.txt`, `undeclared_mcp_tools.txt` and `undeclared_console_areas.txt`. A test compares each one with reality in both directions:
- a new undeclared item fails ("declare it");
- a declared item still listed fails ("remove the line").

When a list is empty, the stream deletes it and turns the rule into a hard requirement: `BOSS_AUTHZ_COVERAGE=fail` by default, `ToolSpec.permission` required, and `AREA_ACCESS` a total `Record`.

---

## 3. Who implements what

| Stub | S1 AuthN | S2 AuthZ | S3 Agents | S4 Console | S5 Audit/Migration |
|---|---|---|---|---|---|
| `authn.*`, `http.fetch`, `ratelimit.hit` | ● | | | | |
| `authz.*`, `scope.register_object_kind` loaders | | ● | | | |
| `delegation.*`, `vault.*` | | | ● | | |
| `audit.record/verify_chain/redact`, `legacy.*`, `policy.get_policy` | | | | | ● |
| `areas.ts` declarations; `@permit` on routes; `ToolSpec.permission` | | ● | | | |
| `<Can>`, `useCan`, `guardedAction`, `<AccessDenied>`, `<StepUpSheet>`, `<ConsentSheet>` and every screen | | | | ● | |

Calls across streams go only through the frozen signatures. Until the other side lands, each stream tests against a fake of the frozen interface. Every module accepts `db=`, so fakes are plain functions.

---

## 4. The five streams

The adversarial test ids are IDENTITY.md §15.3 (A1–A80, A13b, and the A81–A92 added with D26–D33). The journeys are J1–J15, the coverage tests R1–R9, the migration rehearsals M1–M4. Every test file a stream adds is prefixed `test_identity_<stream-topic>_…`, or `identity-…` in apps/web, so names never collide.

### S1 · AuthN: people, credentials, sign-in
**Scope:**
- users, argon2id passwords (with profiles and the semaphore), sessions (rotation, idle and absolute expiry, CSRF), TOTP, passkeys, recovery codes, step-up;
- invitations; the claim of the first Owner on all three paths (setup token, legacy password, network bridge with origin binding, §4.12);
- magic link and email code (§4.14), social presets and self sign-up (§4.15), SSO console sign-in (OIDC RP, SAML SP), SCIM rewired to `sys_scim_*` keys;
- PATs, service accounts and API keys (format, checksum, rotation with grace);
- lockout, rate limits and anti-enumeration; the SSRF-safe client.

**Files it owns (creates or edits):**
- `engine/boss_engine/identity/authn.py`, `http.py`, `ratelimit.py`;
- new modules `engine/boss_engine/identity/{passwords,sessions,totp,webauthn,recovery,invitations,claim,email_signin,social,sso_oidc,sso_saml,scim,api_keys,service_accounts,users}.py`;
- `apps/api/app/routers/identity_auth.py` (`/v1/auth/*`, new), `identity_me.py` (exists: `/v1/identity/me*` including sessions, MFA, passkeys, recovery codes, password, identities, own API keys), `identity_people.py` (new: `/v1/identity/users*`, `/sessions*`, `/service-accounts*`, `/api-keys*`, `/providers*`), `identity_scim.py` (new: `/scim/v2/*`);
- the existing `engine/boss_engine/services/self_hosted_identity.py`, `services/oidc.py` and the SAML services;
- the `boss identity setup-link` CLI command.

**Implements:** `authn.authenticate` (calls S3's `delegation.verify_run_token` for run tokens, and S5's `legacy.bridge` when no new-style credential is present), `parse_api_key`, `hash_token`, `check_csrf`, `http.fetch`, `ratelimit.hit`.

**Consumes:** `authz.check` and `effective_permissions` (S2) for `/me/permissions` and the invite subset rule, `audit.record` (S5) for every event in §8.2, `policy.get_policy` (S5), `vault.store` (S3) for TOTP and OIDC secrets, `delegation.revoke_cascade` (S3) on disable/delete.

**Tests:** A1–A30, A61–A72, A81–A92; J3–J7 (API half); unit tests per §15.1 (argon2id, TOTP, WebAuthn, sessions); the network-bridge claim (origin bound, 404 after the first Owner).

**Done when:**
- every listed test is green;
- `authenticate` resolves sessions, PATs, service-account keys, SCIM keys and run tokens, and falls back to the legacy bridge;
- `/v1/identity/me` returns `MeResponse`;
- every `/v1/auth/*` route is `@public(reason=AUTH_ENDPOINT, verifier=…)` and passes A15/A81;
- no secret appears in a response, log or redirect (A80 sweep passes for S1's routes).

### S2 · AuthZ: the decision, roles, and declaring everything
**Scope:**
- `authz.check` per §6.6 (catalogue → liveness → system sets → base set → step-up → decision);
- the per-`(principal, scope, epoch)` cache with LISTEN/NOTIFY, `bump_epoch`, `explain`;
- the roles, groups and bindings services, including custom roles with versions, the diff and impact preview, the subset rules of §6.11, and last-Owner protection (the trigger exists);
- object loaders for every `Scope.of` kind;
- the **retrofit**: `@permit`/`@public`/`@authenticated` on all 1,162 undeclared operations (hand-checked against IDENTITY_PERMISSIONS.md §6, in the six router groups of IDENTITY.md §13.2 C), filtering of composite views per section, `permission=` on all 114 tools plus the real dispatch check in place of `shadow_tool_call`, `AREA_ACCESS` for all 35 areas, and `FRAMEWORK_DECLARATIONS` for `/openapi.json` and `/docs`.

**Files it owns:**
- `engine/boss_engine/identity/authz.py`, new `engine/boss_engine/identity/{roles,groups,bindings,epoch,object_kinds}.py`;
- `apps/api/app/routers/identity_access.py` (new: `/v1/identity/{groups,access-roles,role-bindings,principals/*/effective,catalogue,seats}`);
- **route declaration lines and handler bodies** in `apps/api/app/main.py` (zone R, §5) and in every `apps/api/app/routers/*.py` that existed at b1befb7d;
- the tool registrations in `engine/boss_engine/tools/*.py` and `engine/boss_engine/mcp/server.py` (the dispatch check);
- `ToolRegistry.invoke`'s identity hook in `engine/boss_engine/tools/registry.py`;
- `apps/web/lib/identity/areas.ts`;
- the three shrinking lists in `tests/identity/`;
- `tests/identity/public_routes.yaml` and `tests/identity/matrix.yaml` (generated from the catalogue).

**Implements:** `authz.check`, `authorize`, `effective_permissions`, `explain`, `bump_epoch`, and every `register_object_kind` loader.

**Consumes:** `audit.record` (S5) for `authz.denied` and `authz.allowed_critical`. The context it decides on comes from S1 (`AuthContext`) and S3 (`RunTokenClaims`).

**Tests:**
- A31–A39; R1 (the shrinking list empties, then `BOSS_AUTHZ_COVERAGE=fail` is the default), R2, R3 (cross-project and cross-org 404 on every object-scoped route), R4 (with S4), R5, R7;
- the role × operation matrix (§15.5) in under 4 minutes;
- J11 and J12 (API half);
- PDP unit tests per §15.1.

**Done when:**
- all three shrinking lists are deleted and coverage is hard-required;
- the matrix is green for 5 roles × 1,164 operations;
- the cross-project suite is green;
- `explain()` feeds `/principals/{id}/effective`;
- every router can be flipped to `enforce` without a shadow-deny for an allowed role.

### S3 · Agents: Passports, delegation, tokens, vault, GitHub, egress
**Scope:**
- the Passport upgrade (`identity_agent_passports`, ceilings from presets, sponsor, spawn rules, depth);
- grants: standing, task-bound JIT and schedule-bound, with live intersection;
- the token service: `mint_run_token`, the RFC 8693 `exchange_token` (checks 1–8 of §7.5), `verify_run_token` with DPoP, the JWKS endpoint and key rotation, introspection;
- the credential shim contract and the run-supervisor handshake;
- the envelope vault (KEK from file or env, KCV boot check, single-flight refresh, provider revocation);
- per-run, down-scoped GitHub App installation tokens, the user-repo-access guard (multi-person installs only, D28), user OAuth for "as me on GitHub", and legacy PATs moved into the vault;
- the `boss-egress` reverse proxy (rule table, ref rules, egress log with partitions);
- the JIT credential fallback (policy-gated);
- `revoke_cascade` with `dry_run` preview, effective in ≤5 s;
- lineage events, jobs carrying `on_behalf_of`, `grant_id` and `chain` (R8), the worker as `sys_worker`, and no MCP token passthrough upstream.

**Files it owns:**
- `engine/boss_engine/identity/delegation.py`, `vault.py`, new `engine/boss_engine/identity/{passports,grants,tokens,dpop,jwks,signing_keys,cascade,lineage,github_app,egress_rules}.py`;
- a new `apps/egress/` (the proxy service);
- `apps/api/app/routers/identity_agents.py` (new: `/v1/identity/{agents,delegations,connections,token,introspect,credentials/github}`, `/v1/identity/me/{delegations,connections}`, `/.well-known/boss-jwks.json`);
- existing `engine/boss_engine/services/agent_identity.py`, the GitHub credential resolution (`repo_token` and `github_*` connection services), the job enqueue paths and `apps/worker/**`, the upstream MCP client code (`engine/boss_engine/services/mcp_*` clients, not `mcp/server.py`);
- the compose and sandbox wiring for the shim and the egress proxy.

**Implements:** everything in `delegation.py` and `vault.py`.

**Consumes:** `authz.check` (S2) at every hop, `authn.AuthnError` (S1), `ratelimit.hit` (S1) for exchange flooding, `http.fetch` (S1) for GitHub, `audit.record` (S5) for lineage, `policy.get_policy` (S5).

**Tests:** A13b, A39–A60, the Keycloak regressions (#52691, #53029, #53040), the JWT BCP set, J8–J10, R8, R9, and the cascade ≤5 s test.

**Done when:**
- an agent run uses no shared key;
- GitHub writes go only through egress;
- the prompt-injection exfiltration test (A51) finds no token;
- the revocation cascade takes ≤5 s end to end;
- `revoke_cascade(dry_run=True)` feeds the console's cascade preview.

### S4 · Console UI
**Scope:**
- every identity screen per the mockups in `~/Downloads/boss-identity-mockup/`:
  - the sign-in sheet (`signin`, reworked 2026-10-06 to D33: mark and welcome, the configured social/SSO icon row hidden when empty, a prominent passkey button, "or use email", tabs Password | Magic link | Email code shown only with SMTP, the self sign-up footer only when enabled, no SMTP-only UI without SMTP, no phone or SMS);
  - claim, including the network-bridge claim with origin confirmation;
  - MFA, passkey, enrolment, recovery codes, step-up and JIT consent;
  - Identity › Overview with posture;
  - People and profiles; Groups; the Access roles editor with impact preview;
  - Service accounts and keys (shown once);
  - Agents and delegations with the narrowing chain and the cascade preview;
  - Sign-in and SSO settings; the Security audit log;
  - My account (security, agents acting for me, API keys);
- the permission mirroring: route guards, rail filtering, `<AccessDenied>` naming the permission and who can grant it, disabled actions with the reason, and `guardedAction` around every server action;
- the Next.js pass-through: forward the cookie, strip `X-Boss-*` and identity headers, and stop using `BOSS_API_KEY` and the HMAC console cookie;
- the CSP.

**Files it owns:**
- all of `apps/web/**`, **except** `apps/web/lib/identity/permissions.gen.ts` (generated), `apps/web/lib/identity/areas.ts` (S2) and `apps/web/lib/identity/contract.ts` (frozen; S4 may add helpers in new files beside it);
- this includes `ConsoleShell.tsx`, `middleware.ts`, `lib/console-session.ts`, `lib/client.ts`, every `*-actions.ts` and every new `app/identity/**` and `app/(auth)/**` page.

**Implements:** `<Can>`, `useCan` (on top of `can()` and `snapshotFrom()`), `guardedAction`, `<AccessDenied>`, `<StepUpSheet>`, `<ConsentSheet>`, and the epoch refetch through the console socket.

**Consumes:** the OpenAPI of S1, S2, S3 and S5's routes. Until those land it builds against a mock server generated from `contracts.py` and the route list in IDENTITY.md §10.

**Tests:**
- J1–J15 as Playwright journeys; each screen is walked by an **independent tester agent** in a real browser before it is reported (BOSS practice);
- R4 (every area, page and action declared; every permission literal in the union);
- J11 (the Viewer walk: no mutating 2xx);
- A10 (CSP; no token in `document.cookie`).

**Done when:**
- every mockup screen exists with parity to the mockup;
- the Viewer walk is green;
- every `*-actions.ts` export is wrapped;
- the rail shows only openable areas;
- the tester's findings are fixed;
- the console holds no shared API key.

### S5 · Audit, migration, rollout, API surface and docs
**Scope:**
- `audit.record` in the caller's transaction (per-org advisory lock for `seq`), `verify_chain`, export with step-up, prune with a checkpoint, daily anchors;
- the one redaction filter, installed at the logging root and in error reporting (A80);
- the security-events API (`/v1/identity/audit*`), posture (`/v1/identity/posture`), and policy (`get_policy`, `GET/PUT /v1/identity/policy`);
- the legacy bridges (`legacy.bridge`): the shared password as Operator for 30 days after the claim (D25); `BOSS_API_KEY`; env keys moved to `legacy_env`; the network bridge (D27: Owner-equivalent before the claim, Operator for at most 30 days after, then refused); the claimed-actor log; `identity_legacy_mappings`;
- M0 seeding: legacy members become pending people, system principals, seats from `default_safety.yaml`, Passport principal rows;
- the shadow report: flushing `enforcement.LEDGER` to `identity_authz_shadow`, and the per-router report;
- the **shadow → enforce rollout** per router (`BOSS_AUTHZ_ROUTER_MODES`), then the global flip;
- the owner recovery CLI (`boss identity recover-owner`), `rotate-master-key` (with S3) and `setup-link` (S1 ships it);
- retiring the legacy middleware and actor helpers in `main.py` zone L (§5) at M5;
- the identity OpenAPI publication; operator docs.

**Files it owns:**
- `engine/boss_engine/identity/audit.py`, `legacy.py`, `policy.py`, new `engine/boss_engine/identity/{redaction,posture,shadow_report,migration,cli}.py`;
- `apps/api/app/routers/identity_admin.py` (new: `/v1/identity/{audit,posture,policy}`);
- `main.py` zone L;
- `engine/boss_engine/cli/**` identity commands;
- `docs/community/**` identity docs, `README.md` sign-in sections, `install.sh` claim-URL printing.

**Implements:** `audit.record`, `verify_chain`, `redact`, `legacy.bridge`, `legacy.note_claimed_actor`, `policy.get_policy`.

**Consumes:** `authz.bump_epoch` (S2), `delegation.revoke_cascade` (S3), and S1's claim service (for the legacy-password and network-bridge claim paths).

**Tests:** A73–A80; M1–M4 (migration rehearsal on a shared-server snapshot: the network-bridge claim with no env change, the bridge ending at 30 days, J14); J2; J15; R6.

**Done when:**
- the chain verifies after export and prune;
- the redaction sweep is clean;
- every legacy path shows in posture with counts and a countdown;
- the shared-server rehearsal passes: claim by passkey, the bridge ends, history is intact;
- every router is in `enforce` with zero unexplained shadow-denies for 7 days;
- `BOSS_AUTHZ_MODE` defaults to `enforce`.

---

## 5. Shared files and how they are split

| File | Rule |
|---|---|
| `packages/identity/catalogue.yaml` and its 4 generated outputs | Lead only, by amendment. A stream needing a permission asks; nobody hand-edits a generated file (the test catches it). |
| `db/schema.sql`, the identity block | Lead only, by amendment. A stream that needs a column asks. Runtime objects (egress partitions) are created by their owner's code, not the schema. |
| `apps/api/app/identity_guard.py`, `engine/boss_engine/identity/{enforcement,declarations,scope,principal,contracts}.py` | Frozen; lead only, by amendment. |
| `apps/api/app/main.py` | **Zone R** (S2): the `@permit`/`@public`/`@authenticated` line above each route, and that route's handler body when its composite view must filter or its `actor` must stop counting. **Zone L** (S5): `require_transport_auth_for_v1`, `require_authenticated_actor_binding`, `require_project_actor`, `require_project_write_actor`, `require_autonomy_operator`, `require_worker_principal`, `govern_actor` and `signed_in_actor`; S5 edits only these functions' bodies, and only at M4/M5. **Lead:** the two identity lines near `app = FastAPI(...)` and the router include plus coverage check before `mount_edition_routers`. Nobody else edits main.py. New identity routes go in the stream's own router file, never in main.py. |
| `engine/boss_engine/tools/registry.py` | S2 (the identity hook in `invoke` and `ToolSpec.permission` becoming required). |
| `engine/boss_engine/config.py` | Lead added `BOSS_AUTHZ_MODE`, `BOSS_AUTHZ_ROUTER_MODES` and `BOSS_AUTHZ_COVERAGE`. Each stream adds its own settings **at the end of the Settings class**, in a block headed with its stream name, so hunks never overlap. |
| `apps/api/tests/conftest.py`, `engine/tests/conftest.py` | Untouched. Identity fixtures (`as_role("operator")`, `run_token_for(...)`) live in `apps/api/tests/identity_fixtures.py`, owned by S2, which the others import. |

---

## 6. Order and integration

1. **All five start now**, each against fakes of the frozen interfaces. Nothing waits on anything to begin.
2. **First integration point:** S1's `authenticate` and S5's `legacy.bridge`. Once both land, shadow mode sees real principals on every request, and the ledger's `not_evaluated` turns into real would-denies. This is the earliest useful merge; aim for it first.
3. **S2's `check` lands next.** Shadow-denies become meaningful, and the S5 rollout can start router by router while S2's retrofit continues (retrofit and rollout go one router group at a time).
4. **S3 integrates** when `check` exists: it is the only stream that needs another's decision to finish its own tests.
5. **S4 integrates continuously** behind its mock server, and switches each screen to the real API as its routes land.
6. **Merge order into `id/identity`:** S1 → S5 (bridge) → S2 → S3 → S4, then each stream's later increments. Nothing merges to main before the operator reviews the whole branch.
7. **Run the tests the change touches during development, and the full suites once, as the final gate** (BOSS practice). The DB-backed suites need a database named like a test one.

---

## 7. Amendments made in Phase 0, and how to ask for one

These deviate from, or decide what was open in, IDENTITY.md as written. They are frozen.

| # | Amendment | Why |
|---|---|---|
| A1 | `PublicReason` gains `preview_capability_url`, `collector_signed`, `loopback_host_only` and `api_description`. | Today's exempt routes (preview proxy, the Improve-BOSS collector, `/v1/bootstrap/seed`, `/openapi.json`) each need a named mechanism. The spec's list could not express them. |
| A2 | The catalogue YAML groups permissions by category, and uses `flags: [...]` (a closed set) instead of one boolean per line. | 180 entries in under 1,200 reviewable lines. The rules are the same. |
| A3 | An `org`-scoped permission takes effect only from an org binding. A role bound at a project still carries it, inert there. A custom role made only of org permissions cannot be bound at project scope. | The spec said org permissions "cannot be bound at project scope", but Admin (bindable at project) holds many. This is the only consistent reading. |
| A4 | `authz.check` is the decision function's name (`authorize` is an alias). Every module takes the database as `db=`. | One name across the codebase. `connection` is reserved for vault connections. |
| A5 | Shadow decisions are **aggregated** (`identity_authz_shadow`, and the in-process `LEDGER` that feeds it). They are not one security event per request. `authz.shadow_deny` is written once per new (route, permission, reason, principal kind) per day. | With 1,162 undeclared routes, a per-request chained event would flood the hash chain. |
| A6 | In enforce mode an evaluation that cannot be made returns `503 {"error":"identity_unavailable"}`, not 403. | Fail closed, and say why: it is not the caller's fault. |
| A7 | The schema uses `TEXT` plus a unique index on `lower(email)` instead of `CITEXT`. It also adds `identity_providers`, `identity_provider_group_mappings`, `identity_vault_keks`, `identity_dpop_replay`, `identity_legacy_mappings` and `identity_authz_shadow`; `system`, `mfa_required` and `cloned_from` on access roles; `self_signed_up` on users; the `magic_link`, `email_code` and `self_signup_verify` challenge kinds; and a default egress partition. `EVENT_TYPES` gains the email sign-in, self sign-up, `legacy.network_used`, `legacy.path_retired`, `catalogue.permission_removed` and `authz.mode_changed` events. | No extension dependency. D26–D32 need them. The spec referenced a KCV and providers without tables for them. |
| A8 | Built-in role ids are `arl_<key>`. Identifiers come from `ids.new_id(prefix)`. | One format, chosen once. |
| A9 | The last-Owner trigger protects an org only after a `bootstrap.claimed` event exists. | An install that has not claimed yet has no Owner to protect, and migrations must be able to seed it. |
| A10 | The route coverage unit is the **operation** (method + path, plus WebSocket and framework routes): 1,164 today, not the 1,144 method+path pairs of the draft mapping. | The boot check must see everything the app serves, including the preview WebSockets and `/openapi.json`. |
| A11 | `@public`'s `verifier` is a reference the coverage test and reviewers use. In Phase 0 the guard does not call it; the handler still does. S2 may move the call into the guard. | Today's verifiers live inside handlers and need the raw body. |
| A12 | The identity API is always enforced, so `/v1/identity/me*` answers 503 until S1 lands. | Fail closed. Nothing in the console depends on these routes yet. |
| S1-1 *(asked by S1; touches `enforcement.py`, lead)* | `evaluate` keeps an `AuthnError`'s own status instead of turning every coded `AuthnError` into 401: `csrf_failed` → 403, `ambiguous_credentials` (cookie + bearer) → 400, as §4.1, §4.3 and A8 say. Today both are refused with 401 before the handler runs; S1's tests accept 401 or 403 at the API and pin 403/400 at `authn`. | The refusal happens either way; the status code is part of the spec's contract. |
| S1-2 *(S1; touches `main.py` lead zone)* | `main.py` includes only `build_identity_me_router()`; S1's other routers (`identity_auth`, `identity_people`, `identity_sso`, `identity_scim`) are included **from** it. The lead may replace that with four explicit `include_router` lines. Router keys are unchanged (`identity_*`, always enforced). | S1 cannot edit `main.py`. |
| S1-3 *(S1; touches `main.py` lifespan, lead)* | One line in the lifespan: `claim.announce_setup_link(db)`, so a fresh install prints its claim URL at boot (§4.11). Until then the link is minted and printed on the first `GET /v1/auth/setup/status` (the console calls it on load) and by `python -m boss_engine.identity.claim setup-link` (S5's `boss identity setup-link` should call `claim.setup_link_command`). | Same reason. |
| S1-4 *(S1; touches `identity_guard.py`, lead)* | The guard does not hand its `AuthContext` to handlers, and the spec's `current_principal` dependency does not exist. S1's handlers authenticate again through `identity_auth.current_context(request, db)` (one more indexed lookup). Proposal: the guard stores the context on `request.state.identity_ctx`, which `current_context` already reads first. The guard's `client_ip` is the socket peer; `authn` re-resolves it through `BOSS_TRUSTED_PROXIES`. | One authentication per request; a real client IP everywhere. |
| S1-5 *(S1; schema, lead)* | `identity_providers` needs `saml_sso_url TEXT` (the IdP's SSO endpoint; S1 stores it in `client_id` for SAML providers until then) and `trusted_authn_contexts TEXT[]` (the SAML AuthnContextClassRef set that counts as MFA under "trust IdP MFA"; S1 uses a fixed list until then). | The spec names both; the frozen table has neither. |
| S1-6 *(S1; new files)* | Beyond §4's list S1 adds `identity/authn_support.py` (server key, client IP, public origin, challenge store, identity mail), `identity/signin.py` (the sign-in flows) and `routers/identity_sso.py`; `routers/identity_sso.py` carries `/v1/identity/providers*` (the spec put them in `identity_people.py`). New wire shapes: `POST /v1/auth/invitations/accept {token, …}` (the token is never in a URL path), `POST /v1/auth/signup/verify`, `GET /v1/auth/methods` (what the sign-in page shows, D33), `GET /v1/auth/setup/status`, `POST /v1/identity/me/mfa/totp/confirm`, `POST /v1/identity/me/passkeys/options`, `/v1/identity/invitations`, `/v1/identity/service-accounts/{id}/disable|enable`; `/saml/v2/metadata` and `/saml/v2/acs` at org level (provider in the query/RelayState). `POST /v1/auth/logout` is `@public(auth_endpoint)` so an expired session can still clear its cookie (it only ends the session the cookie names). | Tokens never in URLs (A88, A92); everything else is additive. |
| S1-7 *(S1 and S5; config)* | `Settings.legacy_console_password` (alias `BOSS_CONSOLE_PASSWORD`) exists in both S1's and S5's blocks with the same name and alias: keep one copy at merge. Community compose now passes `BOSS_CONSOLE_PASSWORD` and `BOSS_PUBLIC_URL` (defaulting to `BOSS_CONSOLE_PUBLIC_ORIGIN`) to the API. With S5 merged, the claim uses `legacy.check_shared_password` and calls `migration.on_claimed` in the claim's transaction (both looked up at run time, so S1 alone still works). | The API must check the claim credential; one setting, not two. |
| S1-8 *(S1 → S5, zone L)* | Under `BOSS_REQUIRE_API_AUTH=true` the legacy transport middleware answers 401 to `/v1/auth/*`, `/v1/auth/sso/*`, `/saml/v2/acs` and `/scim/v2/*` before the identity guard sees them. Zone L must exempt them (each route authenticates by what it is). | Otherwise sign-in is unreachable on a default compose install. |
| S1-9 *(S1 ↔ S4, console contract)* | S1 serves the console's shapes: `GET /v1/auth/options` (AuthOptions: providers, smtp, signup, legacy_password, claim {path, origin, expires_at, seats}); SignInStep `{next: mfa\|done\|enrol, methods: [passkey, totp, recovery], who}` on every sign-in answer; `GET /v1/identity/me/factors` and `/users/{id}/factors` (Factor); `/me/identities` (LinkedIdentity); `qr_svg` in the TOTP enrolment (rendered server-side with segno); `DELETE /v1/identity/me/sessions` (everywhere else); `GET /sessions?principal_id=`; `GET /service-accounts/{id}/scopes`; `PATCH /service-accounts/{id}` with `name`; ApiKey and IssuedSecret `{key, secret, replaces}`, with `replaces` + `overlap_hours` as a rotation; the claim body's `origin` and `seat`. **Two deliberate differences:** an invitation token is never in a URL path — `POST /v1/auth/invitations/preview {token}` and `POST /v1/auth/invitations/accept {token, name, password}` replace S4's `GET /v1/auth/invitations/{token}` and `…/{token}/accept` (access logs keep paths; A88/A92's rule). The invite body stays `{email, bindings[], group_ids[]}`, one person per call (S4's stub sends `{emails[], roles[], groups[]}`). | The console was built on a stub; these are the real routes. |

**To ask for an amendment:** write it as a row in this table (what, why, which streams it touches) and send it to the lead. The lead makes the change in one commit, with its regenerated outputs and the test it needs. Streams rebase onto it. A stream never edits a frozen file itself.

---

## 8. Integration (lead, branch `id/integration`)

The five streams merged in order (S1 → S5 → S2 → S3 → S4), then three follow-up branches (int-a: S3 follow-ups; int-b: S2's holes; int-c/c2: the console on the real API). Amendments the lead made, each with its test:

| # | Amendment | Why |
|---|---|---|
| I1 | The guard authenticates once and keeps the AuthContext on `request.state.identity`; handlers and the legacy transport reuse it (`identity_guard.request_auth`); it sets `lineage.enter(ctx)` and `CURRENT_TOOL_AUTH`. | S1-4: a DPoP proof must never be spent twice; jobs and tools need the acting principal. |
| I2 | `Outcome` keeps an `AuthnError`'s status: 403 `csrf_failed`, 400 `ambiguous_credentials`. | S1-1. |
| I3 | Zone L: identity `@public` routes are exempt from the legacy transport; a NEW credential (session, `boss_` key, run token) is authenticated by identity; the legacy checks see the principal's legacy seat (the Owner and the bridge people act as the console's member) and defer to the guard once a route is enforced (`IDENTITY_ENFORCED`). The org picker and project list follow identity under enforce. | S1-8 and the console no longer carrying a shared key: sign-in was unreachable and an invited person saw no org. |
| I4 | Epoch triggers live in `schema.sql`; `identity_access_requests` + `access.requested` (A13); the vault master key is a file on its own volume, created once (O_EXCL, 0600); the server key and audit HMAC derive from it; boot checks the KEK, provisions signing keys and Passports. | Persistent keys without an installer step (install.sh untouched); A53 was never called. |
| I5 | The vault serves S1's `totp.verify` and `sso.client` purposes. | TOTP sign-in and OIDC login were 500s against the real vault. |
| I6 | Passport presets match the persona callsign; an empty ceiling is repaired once; Passports are made at the claim and lazily (in their own transaction); an un-passported agent is refused under enforce. | Every shipped agent had an empty ceiling, so no grant could name it and builds silently ran outside delegation. |
| I7 | A run's consent asks for each step's tool permission and brings the step's capability captain. Revoking a grant also stops a queued or running build under it. | The enforce-mode walk: a build step was refused mid-run; a revoked build ran on. |
| I8 | 500 bodies carry the class and request id, never the message; the deferred last-Owner trigger answers 409 `last_owner` on every path. | S5's flag; a SCIM delete gave a 500. |
| I9 | A passkey-first claim needs no password. | The console's passkey path sent none and the claim answered 422. |

### 8.1 Round 2: the three security fixers (F1 agents, F2 authorization, F3 authentication)

Merged F1 → F2 → F3 (conflicts: conftests, compose, social.py, posture/console shapes). Gaps closed between them:

| # | Change | Why |
|---|---|---|
| R2-a | The vault boots before the claim link; the first Owner's claim provisions the signing keys; compose documents first-install key creation. | server_key() fails closed (F3) and the key file is made only at first install (F1): a fresh compose install printed no claim link and had no signing keys until a restart (the org is seeded after the API boots). |
| R2-b | Promotion into an MFA-required role (binding or group) opens a first-factor enrolment window (72 h, consumed at the next sign-in). | Enrolment-only sessions (F3/H1) stranded promoted people. |
| R2-c | SCIM group membership writes carry the user-write limits (own provisioned people only, never Owner/Admin) and never add to a group bound to Owner/Admin. | F3/M2 covered user writes only. |
| R2-d | The MCP bridge presents a DPoP-bound `boss:api` run token of the caller's run (its shim, or a first-hop sibling); deeper chains are refused. | Agent runs could not use API-surface tools (F2/M8 fails closed). |
| R2-e | Work with no grant clones as BOSS's audited read-only system agent (`agt_sys_repo_reader`, autonomous, Viewer, read-only GitHub ceiling); under enforce without egress it is refused. | The shared-token fallback (F1 leftover). |
| R2-f | Under enforce a legacy API key acts as its legacy principal, never the `ceo` seat. | Actor attribution. |
| R2-g | Legacy SCIM group capabilities are not imported as bindings; they are written on the group for Owner review, and earlier seeded bindings are removed. | The old shared SCIM bearer could set them. |
| R2-h | A sandboxed process starts from the agent allow-list, never the control plane's environment. | DATABASE_URL reached the sandbox when a caller passed no env. |
| R2-i | Egress answers git's first request with `WWW-Authenticate: Basic`; the bare handshake is not audited. | Found on the compose stack: every clone and push through egress failed. |
| R2-j | The audit log's `actor` filter is exempt from the actor rule; Verify opens the step-up sheet. | Found on the compose stack: the Owner's audit log was empty. |
