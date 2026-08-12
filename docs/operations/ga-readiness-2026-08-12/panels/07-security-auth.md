# Panel 07 — Security / Auth / RBAC (GA)

| Field | Value |
| --- | --- |
| **Panel** | Security / Auth / RBAC Expert |
| **Date** | 2026-08-12 |
| **Scope** | Transport auth (API keys, OIDC, SAML session), `require_project_actor`, SecretRefs, GitHub credentials, tenant isolation, dual-control approvals, enterprise SSO gating, dependency exposure |
| **Primary sources** | `apps/api/app/main.py` (transport middleware + actor binding), `engine/boss_engine/services/{oidc,saml,members,break_glass,secret_refs,github_connections,github_merges,resource_plane,enterprise,redaction}.py`, `engine/boss_engine/config.py`, `packages/specpack/policies/default_safety.yaml`, `packages/specpack/objects/enterprise_identity_rollout_policy.yaml`, `.ai/security-review.md`, `docs/operations/WEBHOOKS.md`, `deploy/kubernetes/base/configmap.yaml`, live dogfood gate `auth.unauthenticated_rejected` |
| **Verdict** | **CONDITIONAL** |

---

## Executive summary

BOSS’s security *model* is GA-capable: fail-closed transport when auth is required, principal binding against actor spoofing, project/org RBAC with audited denials, SecretRef-only credential surfaces, dual-control for high-risk ChangeSets and independent merge authorization, and enterprise SSO/SCIM gated behind explicit per-project rollout policies (metadata does not imply live auth). Live dogfood (2026-08-12) rejects unauthenticated `/v1` with 401.

The gate is **CONDITIONAL**, not PASS, because several production hardening items remain operational or small design gaps rather than closed release gates: default `BOSS_REQUIRE_API_AUTH=false` outside k8s/goldeneye, in-memory rate limits only, dependency audit is advisory (not CI fail-on-high), JWKS client has no explicit timeout, ChangeSet dual-control currently matches **member_id strings** to skill approver role names (works for seeded `ceo`/`cto`, fragile for custom members), and engine `SECURITY.md` still lacks a public vulnerability contact (expected pre-public release).

No local P0 secret-leak or auth-bypass defects were found in this review. External enterprise IdP live cutover remains correctly blocked until rollout policies + secrets are configured.

---

## Assessment matrix

| Control area | Status | Evidence | Residual risk |
| --- | --- | --- | --- |
| Auth fail-closed | **PASS** (when required) | Middleware forces 401 without valid API key / OIDC / SAML session when `require_api_auth` **or** OIDC **or** enterprise SAML enabled; 503 if auth required with no keys and no OIDC/SAML; unbound API keys 401 when principal map configured | Dev default `require_api_auth=false` can leave open local/API if mis-deployed outside validated k8s/helm/goldeneye profiles |
| Principal / actor binding | **PASS** | `require_authenticated_actor_binding` before permission checks; bounded role-key delegation only; tests for impersonation block + OIDC binding | Delegation path is intentional (`executive_console` + role_keys); operators must understand role-key binding |
| RBAC + denials | **PASS** | `require_project_actor` → member + permission; break-glass TTL grants with audit; policy denials emit `policy.denied` (audit write failures logged, still 403) | Default-member self-heal re-seed is narrow and intentional |
| Secret leakage | **PASS** | SecretRef metadata-only CRUD; env keys validated `^[A-Z_][A-Z0-9_]*$`; raw material rejected in metadata; GitHub connections store `credential_ref` only; redaction helpers for data-boundary text; provider credential tests assert no raw persistence | Ensure new LLM/review paths keep calling redaction; ops must never log `BOSS_API_KEYS` |
| Tenant isolation | **PASS** with notes | Queries and SecretRefs scoped by `project_id`; org members resolved via workspace→org join (same-org only); cross-project secret/ref lookups fail closed; multi-tenant personal subscriptions denied by entitlement policy | Not a full multi-tenant SaaS control plane; goldeneye is single-tenant dogfood — isolation is project/org RBAC, not hard network tenancy |
| Dual-control approvals | **CONDITIONAL** | ChangeSet skill `requires_approval: [ceo, cto]` accumulates multi-approver list; endpoint requires `role_keys_any` for dual-control skills; GitHub merge auth forbids self-approval; risk tiers in `default_safety.yaml` (`2`→ceo, `3`→ceo+cao) | Approvals recorded as **actor member_id** must equal role token for completion; custom member_ids with role_keys may stall dual-control |
| Enterprise SSO gated | **PASS** | IdP metadata ≠ runtime; `EnterpriseIdentityRolloutPolicy` modes `disabled` / `metadata_only` / `auth_allowed` / `provisioning_allowed`; SAML ACS + SCIM fail closed without policy; OIDC enable without issuer/aud/JWKS → 503 | Live IdP/SCIM still external-blocked (expected); JWKS fetch timeout not explicit |
| Webhooks / machine auth | **PASS** | GitHub/Plane/Bitbucket/reliability webhooks auth-exempt but HMAC/signature gated; missing secret → 503, no payload store; invalid sig → 401 | Preview `/v1/previews/.../web|feedback|logs` relies on unguessable id + TTL (by design) |
| Known dependency vulns | **CONDITIONAL** | `make deps-audit` reports only; historical pnpm audit: **dev-only** vitest/esbuild/vite/postcss issues; runtime Python stack not fail-gated in CI | Need pip-audit + pnpm audit **fail-on-high/crit** in release CI |
| Live dogfood auth | **PASS** | `live_dogfood_gate.json`: `auth.unauthenticated_rejected` required check OK (401) | — |

---

## Detailed findings

### 1. Transport authentication (fail-closed)

Implementation: `require_transport_auth_for_v1` in `apps/api/app/main.py`.

- Auth is required when **any** of: `BOSS_REQUIRE_API_AUTH`, `BOSS_OIDC_ENABLED`, `BOSS_ENTERPRISE_SAML_ENABLED`.
- API keys accepted via `Authorization: Bearer` or `X-BOSS-API-Key` with constant-time compare.
- If principal map (`BOSS_API_KEY_PRINCIPALS`) is non-empty, keys without a bound member are **401** (fail-closed).
- OIDC: JWKS signature + `iss`/`aud`/`exp`/`sub` required claims; unresolved principal → 401; enabled but incomplete config → **503**.
- SAML session tokens only when enterprise SAML enabled and session authenticates.
- Exempt paths (signature-gated or browser-preview): inbound webhooks, GitHub/Plane/Bitbucket webhooks, reliability alerts, preview web/feedback/logs.

**Deploy defaults**

| Profile | Auth required |
| --- | --- |
| Code default (`config.py`) | `require_api_auth=false` (local DX) |
| K8s base ConfigMap | `BOSS_REQUIRE_API_AUTH: "true"` (validated by `scripts/validate_kubernetes_manifests.py`) |
| Goldeneye compose | `${BOSS_REQUIRE_API_AUTH:-true}` |

**Must-fix for any GA deploy profile that is not pure local-dev:** enforce `BOSS_REQUIRE_API_AUTH=true` **and** non-empty `BOSS_API_KEYS` with principal bindings (or OIDC fully configured). Helm/k8s validators already encode this for the Kubernetes base.

### 2. Authorization (`require_project_actor`)

Order of checks (correct, anti-oracle):

1. Authenticated principal binding (impersonation → 403 + denial event).
2. Active project or same-org member with permission (or active break-glass grant).
3. Optional default-member seed self-heal only for known system member ids, then re-check.

Org members cannot escalate across orgs: resolution joins `projects → workspaces → orgs`. Project-level collaborator upsert does **not** overwrite arbitrary org grants (DEFAULT_MEMBER_IDS mirror is insert-only).

### 3. Secrets & GitHub credentials

- **SecretRef**: metadata CRUD only; `require_usable` enforces status/rotation overlap; rejects raw-looking metadata keys.
- **GitHub connections**: registry stores SecretRef pointers + non-secret installation identity; adapter resolves secrets at execution time only.
- **Webhooks**: missing secret → 503 without parsing; unverified payloads not stored.
- **Model / provider credentials**: entitlement paths require SecretRef binding; personal subscription multi-tenant workloads denied.
- **Redaction**: deterministic pattern mask for AWS keys, bearer tokens, private keys, etc., for data-boundary content.

No hardcoded production secrets in tree; `.env.example` documents blanks only.

### 4. Dual-control & independent approval

| Path | Behavior |
| --- | --- |
| ProviderOps ChangeSet | Skill policy list `requires_approval` → multi-role accumulation; partial approvals leave ChangeSet pending; apply requires approved status + budget/receipt invariants |
| ChangeSet approve API | Dual-control skills: `role_keys_any` on required roles + principal binding (no generic `providerops_manage` shortcut) |
| GitHub merge authorization | Requester cannot self-approve (`independent approver`); evidence + policy version checks |
| Risk tiers (`default_safety.yaml`) | Tier 2 → ceo; tier 3 → ceo+cao |
| Destructive provider / prod DNS-deploy | Spec + resource plane require approval + rollback plan |

**Gap (must-fix or explicit GA acceptance):** dual-control completion checks `required_role in approvals` where `approvals` stores **actor member_id**. Seeded members use `member_id == role` (`ceo`, `cto`). Custom humans with `role_keys: [ceo]` but `member_id: alice` can be authorized by `role_keys_any` yet never satisfy the role-token membership test. Fix by recording satisfied **roles** (from membership) rather than raw actor id, or document that dual-control approver identities must use role-aligned member_ids until fixed.

### 5. Enterprise SSO / SCIM gating

- Provider records support OIDC / SAML / SCIM metadata + SecretRef for SCIM tokens.
- Runtime uses `identity_rollout_status(..., required_mode)`:
  - Missing / inactive policy → disabled.
  - `metadata_only` does not allow auth or provisioning.
  - SAML auth requires `auth_allowed`; SCIM provisioning requires `provisioning_provisioning_allowed` / `provisioning_allowed`.
- Events: `enterprise.saml_runtime_blocked`, login failures audited.
- Transport OIDC is separate (deployment-level `BOSS_OIDC_*`) from enterprise IdP catalog; both fail closed when misconfigured.

**Enterprise live SSO is not a code gap** for Community GA; it is an external configuration gate already tracked (GAP-2026-06-05-003).

### 6. Rate limiting & abuse

- Sliding-window in-memory limiter on high-cost paths (`/v1/metatron/query`, `/v1/commands/propose`, `/v1/resources/sync`); 429 + `rate_limit.exceeded` event.
- Documented as local/test-class; multi-replica production needs Redis/edge enforcement.
- Comms outbox throttle and entitlement rate states exist separately.

### 7. Dependency / known vulns

- `make deps-audit`: freeze sample + pnpm audit report; **does not fail** the build.
- Prior review: no known high runtime Python vulns from manual freeze; web **critical** vitest UI and **moderate** esbuild/vite/postcss are **dev/test toolchain** (not Next production runtime).
- Engine `SECURITY.md`: private reporting contact **not yet configured** — acceptable while private; **must-fix before public OSS GA**.

---

## Tests & live evidence (security-relevant)

| Evidence | Result |
| --- | --- |
| `test_v1_routes_fail_closed_when_auth_required_without_configured_keys` | 503 |
| `test_api_key_principal_binding_*` / unbound key | 403 / 401 |
| `test_oidc_principal_binding_*` | binding + anti-impersonation |
| Provider credential / SecretRef tests | raw secrets rejected; refs only |
| `test_github_merge_authorizations` independent approver | 409 self-approval |
| AWS ChangeSet dual-control tests | pending until multi-role |
| Live dogfood `auth.unauthenticated_rejected` (2026-08-12) | PASS |
| K8s manifest validator | requires `BOSS_REQUIRE_API_AUTH=true`, secrets via `boss-secrets` |

---

## GA security gate

### Verdict: **CONDITIONAL**

Ship **internal / goldeneye / controlled GA** only when the must-fix checklist below is signed off per environment. Do **not** claim unconstrained multi-tenant public enterprise GA until dual-control role accounting, distributed rate limits, and CI dependency fail-gates are closed (or formally accepted with compensating controls).

### Must-fix list (blockers for security GA sign-off)

1. **Deploy auth hard-on** — Every non-local GA environment: `BOSS_REQUIRE_API_AUTH=true`, non-empty rotated `BOSS_API_KEYS`, and `BOSS_API_KEY_PRINCIPALS` (or fully configured OIDC with JWKS). Confirm with unauthenticated 401 probe (dogfood gate already models this).
2. **Principal binding on all service keys** — No unbound keys in principal-map mode; worker/console keys map to least-privilege members.
3. **Dual-control correctness** — Fix ChangeSet multi-approver accounting to record **roles satisfied** (not only actor id), **or** freeze GA ops runbook: dual-control approvers must use role-aligned member_ids (`ceo`/`cto`/…) and verify with an integration test for a custom member_id.
4. **Dependency audit gate** — Add CI `pip-audit` (or equivalent) + `pnpm audit` with **fail on high/critical** for production dependency graphs; keep dev-only advisories as allowed exceptions with documented waiver.
5. **Production rate limiting** — Replace or front the in-memory limiter with Redis/edge limits on high-cost and auth endpoints for multi-replica deploys.
6. **OIDC JWKS resilience** — Explicit JWKS HTTP timeout + fail-closed behavior under IdP outage (no hang open).
7. **Public security contact** — Before public/OSS GA: populate `engine/SECURITY.md` (and root policy) with private reporting channel and response targets.
8. **Webhook / preview secrets** — Production must set GitHub/Plane/inbound webhook secrets; preview paths remain TTL + unguessable-id only — document threat model acceptance.
9. **Enterprise SSO cutover (if claiming Enterprise GA)** — Active IdP, SCIM tokens as SecretRefs, explicit `auth_allowed` / `provisioning_allowed` rollout policies, and release-blockers green; until then keep fail-closed (current correct default).

### Acceptable residual (non-blocking if documented)

- Live external Account Connect / live provider destructive writes remain fail-closed without hosted config and infra flags (product-correct).
- Dev-only npm toolchain advisories under vitest/vite when not shipped to production images.
- Single-tenant dogfood topology vs future multi-tenant SaaS plane (isolation today is org/project RBAC + SecretRef scope).

### Sign-off criteria for upgrade to **PASS**

- Must-fix items 1–6 closed with automated tests or deploy validators.
- Fresh `make deps-audit` / CI audit green under fail-on-high policy.
- Live environment shows: unauthenticated 401, impersonation 403, SecretRef-only provider connect, dual-control ChangeSet cannot single-actor complete for dual-role skills, merge self-approval denied.
- Security contact published if public release is in scope.

---

## Mapping to hard rules (AGENTS.md)

| Hard rule | Status |
| --- | --- |
| No direct external billing/infra/Slack/GitHub/model without adapter boundary | Observed in reviewed paths |
| Receipt + `dedupe_key` for external effects | Enforced in webhooks/provider flows |
| BudgetReservation before spendy actions | Present on provider apply / model paths |
| Worker lease + TTL | Out of deep scope this panel; not contradicted |
| ChangeSet + ResourceReceipt for provider writes | Enforced |
| Destructive / prod DNS-deploy need approval + rollback | Spec + resource plane |
| Failures visible (retry / deadletter / decision) | Policy denials + webhook deadletters |
| No autonomous self-mod (PR/config only) | Policy `self_improvement.apply_directly: false` |

---

## Recommendation

**CONDITIONAL GA security gate.** Architecture and dogfood evidence support controlled GA. Close deploy-auth, dual-control role accounting, CI dependency fail-gates, and distributed rate limits before marketing unconstrained multi-tenant or public enterprise security claims.

**Panel owner:** Security / Auth / RBAC Expert Panel  
**Next review trigger:** After must-fix PR(s) or first multi-replica production cutover.
