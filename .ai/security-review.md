# BOSS Security Review (Autonomous)

**Date**: 2026-06-05
**Reviewer**: SECURITY ENGINEERING AGENT
**Scope**: auth, authz, secrets, input val, data exposure, injection, dep risks, logging, rate limits, abuse, secure defaults.

## Positive Findings (incl recent closures)
- Bootstrap seed: now disabled by default (BOSS_BOOTSTRAP_SEED_API_ENABLED=false), requires explicit enable + member_manage perm + emits audit. No unaudited success. (Closed GAP-007)
- Policy denial audit: failures to write denial events now produce structured logs with context (project, actor, perm, entity, reason) instead of silent swallow. Fail-closed 403 preserved. (Closed GAP-001)
- Auth: API key (Bearer or X-BOSS-API-Key) or OIDC JWT with issuer/aud/JWKS + principal claim mapping to project member. Principal binding prevents actor spoof when configured. Multi-tenant project/org RBAC enforced server-side.
- Break-glass: explicit grants, revocable, org-delegate support.
- Secrets: SecretRef only to agents; adapters hold real creds locally or via env. No raw to prompts. .env.example only (never commit real).
- Data boundary: classification + enforcement in intake/jobs/comms/audit/entitlements; paid routes budget-gated.
- Input: schemas.py + pydantic/FastAPI validation; specpack YAML validated on load.
- Provider writes: always ChangeSet + budget res + approval (risk) + ResourceReceipt + dedupe.
- Audit everywhere for writes + denials.
- No hardcoded secrets in code.

## Risks / Findings to Address
- OIDC/JWKS fetch: if enabled, ensure timeout + fail-closed on bad config (current code does).
- Rate limits: not explicit in all paths; add for public-ish or high-cost (Metatron, command, provider sync) if abuse vector.
- Deps: run pip-audit / npm audit / pnpm audit; review in CI.
- Logging: ensure no secrets in logs (structured audit ok, but filter).
- Error messages: safe (no stack or internal to clients on 4xx/5xx).
- SCIM/IdP endpoints: bearer token gated, but live config Blocked.
- Account Connect: local fails closed if no hosted.
- Web client: sends key from env; XSS? Assume Next.js safe, but review forms.
- No major injection (SQL via ORM/psycopg params, no raw).

**P0s Closed This Cycle**: bootstrap gate, denial logging.
**Remaining**: None local P0; external for full enterprise/commercial (expected).
**Recommendations**:
- Add dep scan to Makefile or CI if absent (scripts/check_external... exists but no net).
- Explicit rate limit middleware for mutation paths.
- Expand security tests for perm bypass attempts.
- Review recent changes (kpi, budget consume) for any new surface (they use existing perm patterns + pack_manage/budget_manage).

**Status**: Strong secure defaults + recent P0 fixes. PASS for local with notes for scale.
**Commands**: make lint (includes ruff), pip-audit if installed, pnpm audit --filter boss-web.

## Follow-up Security Review (post PR #3 / codex/spec-closure-audit, autonomous 2026-06-05)
**Date of this pass**: 2026-06-05
**Commands executed** (per task):
- make lint (equiv via pnpm --filter boss-web lint + .venv/bin/python -m ruff check; full `make lint`): exit 0, tsc clean, ruff "All checks passed!".
- python -m ruff: 0.12.0, clean on apps/api apps/worker.
- pip list (via .venv/bin/python -m pip list --format=freeze): full list captured (runtime: fastapi==0.115.12, pydantic==2.13.4, openai==1.93.0, cryptography==48.0.0, psycopg==3.2.9, uvicorn==0.34.3, PyJWT==2.10.1, SQLAlchemy==2.0.41, PyYAML==6.0.2, ruff==0.12.0, etc. No ancient vulns visible in core; certifi dated 2026.5.20 in env). .venv/bin/python -m pip-audit: not available (not installed; "Package(s) not found").
- pnpm audit (from apps/web): reported dev-dep advisories only (no prod runtime):
  - esbuild@0.21.5 (moderate, GHSA-67mh-4wv8-2f99): dev server CORS allows cross-site read of bundles/SSE in vite/vitest dev mode. Patched >=0.25.0. Paths via vitest@2.1.9 > vite@5.4.21.
  - vite@5.4.21 (moderate, id 1116229): related.
  - postcss (moderate, id 1117015): via next.
  - vitest@2.1.9 (critical, GHSA-5xrq-8626-4rwp): "When Vitest UI server is listening, arbitrary file can be read and executed" for <4.1.0. Patched >=4.1.0. Dev/test only (vitest UI).
  Note: These are dev/build/test tooling (used in `pnpm test`, `make lint`/`build`); production Next.js build does not ship vitest/vite dev server. No action without approval per rules. pnpm audit run from web dir succeeded in reporting.
- grep (via tool + terminal, excluding node_modules/.git/tmp/artifacts/*tsbuildinfo/tests where noted):
  - For "TODO sec or rate_limit or secrets": 0 TODO/FIXME with sec keywords in core src (apps/api/app/{main,config,schemas,services/*}.py , apps/web/app/{actions,page}.tsx lib/*.ts except test mocks/labels). rate_limit only in: comms throttle (outbox), entitlement capacity states ("rate_limited"), health/rate_limit_status fields (schema, openapi, api-client, web display, provider health). No rate limiting middleware on API paths. Secrets: only SecretRef fields, "env:XXX" credential_refs in examples, UI labels/placeholders, test overrides (never committed values or raw logs).
  - No matches for dangerous patterns (eval/exec/pickle/unsafe yaml) in non-test non-script code.
- Specific recent surfaces audit (KPI create, budget consume, bootstrap):
  - Locations: apps/api/app/main.py:3354 (create_kpi), 5575 (bootstrap_seed), 5764 (consume_budget); schemas.py:1781 (KpiCreateRequest), 1116 (BudgetConsumeRequest); services/canonical.py:103 (create_kpi impl); services/budget.py:656 (consume); bootstrap.py:10 (seed); config.py:76 (flag default).
  - Input validation: Strong. Pydantic models with Field(gt=0/ge=0) for values, required project_id etc, actor. Server-side in canonical: _require_slug(kpi_key), strip, target>0, current>=0, role existence check. In budget.consume: explicit >=0 (redundant but safe), assert active reservation + receipt exists for project (by id). Bootstrap: no payload beyond actor query param (default "ceo").
  - Permissions: Enforced. create_kpi: require_project_actor(..., "pack_manage") before call. consume: require_project_actor(..., "budget_manage"). bootstrap: if enabled + project exists, require "member_manage"; always pre-checks flag (default false from Field).
  - No secret logging / exposure: Errors use str(controlled ValueError) only (e.g. "target_value must be greater than zero.", "Active budget reservation not found."). Denial paths call record_policy_denial / record_budget_denial_event which log structured (project/actor/permission/reason/receipt_id etc) + emit "policy.denied" / "budget.*denied" events; LOGGER.exception only for audit-write-fail with same non-secret context. No tokens/keys/passwords in any log strings, f-strings, or returns. Seed returns only {"status": "seeded", "actor": actor} after event. Adapters/SecretRef boundaries preserved (no new surfaces touch raw).
  - Safe errors: Yes. 400 for val (KPI), 409 for consume conflict/denial (with denial event), 403 for bootstrap disabled or perm fail (with denial record). No stack traces, no internal paths/DB details, no 500 leaks. HTTPException from FastAPI + explicit. Auth failures handled pre-handler.
  - Tenant scope: All via project_id + require_project_actor (which does members.require_member + authenticated_actor_binding).
  - Other: Uses existing patterns (no bypass of budget res/receipt/dedupe/audit). KPI upsert is intentional (ON CONFLICT project+kpi_key) + emits "kpi.upserted". Bootstrap idempotent inserts + "bootstrap.seed_executed". Consume links to receipt + optional overrun event.
  - Web surfaces (actions.ts createKpiAction, page forms): client-side parseNumber guards (mirror server), calls typed api-client (no direct fetch, no secrets), formData only (no eval). fetchKpiFeed has error->empty state. No budget/consume UI form in current (API only, tested).
  - No new surfaces introduce direct external, missing res/receipts, or self-mod.
- Rate limits: Now addressed (basic in-memory impl per this Security task + P2). Explicit limiter on high-cost: /v1/metatron/query, /v1/commands/propose, /v1/resources/sync (provider sync) + test coverage. 10 req/min sliding per (IP+proj+actor), 429 + rate_limit.exceeded event. Config via BOSS_RATE_LIMIT_ENABLED/RPM (defaults on). In-mem test/local only (per note: prod requires redis/edge per AGENTS/security). Prior absence closed as GAP-2026-06-05-013. (Comms throttle + entitlement rate_limited states pre-existing.) Abuse mitigated for costly paths.
- Dep vulns: As above, noted only dev; runtime clean per manual review of list. Recommend (P2) adding pip-audit to Makefile lint target or release-blockers once tool installed (do not auto-install in this review).
- New gaps identified (P2/P3 only; no new P0s):
  - P2: Explicit rate limiting ... -> CLOSED as GAP-2026-06-05-013 this pass (basic in-mem stdlib impl + 429+event on /metatron /propose /resources/sync; config; tests; openapi; prod note; see code + gap).
  - P2: Integrate dep vuln scanning (pip-audit in py lint, pnpm audit --audit-level in web; fail on high/crit in CI). Note existing pnpm vulns are dev-only.
  - P3: Add targeted perm-bypass + input-fuzz + error-leak tests for KPI/budget-consume/bootstrap in test_api.py (existing coverage for happy/denial but more edge like malformed kpi_key, cross-project, actor spoof post-binding).
  - P3: Review/scan for future secret leakage in new LLM review packets or Metatron answers (current secret_pattern scanner good).
- Overall: Recent surfaces (KPI, full budget consume/forecast, bootstrap) pass sec review: use typed schemas, deterministic perms, fail-closed, auditable denials, no secret exposure, safe errors. Aligns with PRD constitution, AGENTS hard rules, default_safety.yaml (require_receipt/dedupe/res/lease, forbid raw secrets/rate_evasion). No direct external bypasses. State machines tested (pre-existing + gap closures). No P0s introduced. Deps note only. Rate limit P2 addressed/closed this pass (GAP-013) with basic impl.
**Tests/scans run this pass**: ruff, tsc (via lint), pnpm audit, pip list, targeted greps on recent code + whole (excl artifacts), manual code read of endpoints/services/schemas/config/bootstrap + web actions/kpis, schema grep, subprocess pattern grep. + rate-specific: pytest for 429+event on high-cost, make lint. All clean for P0.
**Status this pass**: PASS (local). Rate P2 closed as GAP-013. No new P0s; remaining P2 (dep scan etc) noted. Update agent .md + activity log + gap.

**Prior P0s remain closed; external Blocked as before.**
