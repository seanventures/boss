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
