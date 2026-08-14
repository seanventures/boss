# Phase C2 security status (Community)

**Date:** 2026-08-13  
**Branch:** `ai/grok/community-ga`  
**C2 verdict:** **NOT DONE.** Local-code items below are verified or documented.  
Do not claim Community GA Phase C2 closed while residuals remain.

C2 in [`GA_LIVING_PLAN.md`](GA_LIVING_PLAN.md) is “Security must-fix (principals, dep CI, rate limits).” Panel 07 (`ga-readiness-2026-08-12/panels/07-security-auth.md`) is still **CONDITIONAL**.

---

## Local-code items this pass

### 1. API key principals + SCIM org-member fallback (BOSS-320)

| Check | Result |
|---|---|
| `BOSS_API_KEY_PRINCIPALS` mapping | **Present.** `Settings.api_key_principals` / `api_key_principal_map` in `engine/boss_engine/config.py`. Unbound keys 401 when the map is non-empty (`apps/api/app/main.py`). |
| Existing binding tests | `test_api_key_principal_binding_reports_session_and_blocks_impersonation`, `test_unbound_api_key_fails_closed_when_principal_binding_is_configured`, `test_api_key_principal_binding_applies_to_decision_transitions` |
| SCIM deprovision vs org fallback | **Code already shipped** (Plane BOSS-320 Done, PR #131 / `0bad732`): `upsert_scim_user` inactivates matching `org_members` in the same transaction. |
| Focused regression added | `test_deprovisioned_scim_user_cannot_act_via_org_member_fallback` plants a leftover active org mirror, deactivates via SCIM, then asserts `org_members.status=inactive` and `POST /v1/commands/propose` is **403**. |

### 2. Rate limits (Community-ok in-proc)

| Check | Result |
|---|---|
| Implementation | In-process sliding window in `apps/api/app/main.py` (`InMemoryRateLimiter`). High-cost paths: `/v1/metatron/query`, `/v1/commands/propose`, `/v1/resources/sync`. |
| 429 + audit event | **Already tested:** `test_high_cost_endpoints_rate_limited_429_and_emits_event` asserts HTTP 429 **and** `rate_limit.exceeded`. Adjacent: sync path, disable bypass, audit-write-fail log. |
| Redis | **Not added.** `redis` is the A2A stream transport only — not a rate-limit backend and not half-wired as one. |
| Community posture | Single-process / Community dogfood: **in-proc limiter is the supported control.** Multi-replica or public-edge must front with Redis/LB/CDN (documented residual). |

### 3. Dependency advisories (BOSS-326)

Scan date: 2026-08-13. Pins vs latest PyPI and published GHSAs.

| Package | Pinned | Latest PyPI | Constraint | Disposition |
|---|---|---|---|---|
| PyJWT | `2.13.0` (`apps/api`, `apps/worker`, `engine/requirements.txt`) | **2.13.0** | `engine/pyproject.toml` `PyJWT[crypto]>=2.10,<3` | Already at latest. No bump available. |
| cryptography | `50.0.0` (same manifests) | **50.0.0** | `cryptography>=50,<51` | Already at latest. No bump available. |

PyJWT 2.13.0 includes published fixes for GHSA-752w-5fwx-jx9f (`crit` header, patched 2.12.0), GHSA-xgmm-8j9v-c9wx (HMAC/JWK confusion), and GHSA-jq35-7prp-9v3f (PyJWK algorithm allow-list). Earlier engine pin `2.10.1` was the BOSS-326 finding; PR #139 moved the tree to 2.13.0.

cryptography 50.0.0 includes GHSA-g6cj-pr64-35w5 (PKCS#7 Bleichenbacher, patched 50.0.0), GHSA-537c-gmf6-5ccf (OpenSSL wheels, patched 48.0.1), GHSA-jwv3-5hgf-82ww and GHSA-m2h6-j472-rp4c (path-building / wildcard name constraints, patched ≥49.0.0). Earlier pin `48.0.0` was the BOSS-326 finding.

**No version bump in this pass** — the safe upgrades are already the pinned versions. Expanding `<51` or installing unreleased cryptography would be out of scope.

Plane BOSS-326 is Done with `pip-audit -r engine/requirements.txt` reporting 0 findings after #139. GitHub Actions remain disabled; do **not** wire this into CI in this workstream.

### 4. OIDC JWKS HTTP timeout (closed locally)

`engine/boss_engine/services/oidc.py` now constructs `PyJWKClient(jwks_url, timeout=JWKS_HTTP_TIMEOUT_SECONDS)` with an explicit **5s** timeout. JWKS transport failures (`TimeoutError`, `URLError`, `OSError`, `ConnectionError`) and `PyJWTError` (including `PyJWKClientConnectionError`) fail closed as `OidcAuthenticationError`. Tests: `engine/tests/test_oidc.py`.

### 5. Local fail-on-high runtime Python audit (closed locally)

`make deps-audit` stays **advisory** (freeze + waived npm snippet). New `make deps-audit-strict` installs `pip-audit` as a **tool** (not a runtime pin) and runs `scripts/deps_audit_strict.py` against runtime pins from `engine/requirements.txt`, `apps/api/requirements.txt`, and `apps/worker/requirements.txt`. pytest / ruff / playwright are excluded. High, critical, and unknown-severity findings fail the target. pip-audit JSON has no severity; the script classifies CVSS v3 vectors and enriches unknowns from OSV/GHSA. Dev-only npm/vitest/vite/esbuild/postcss advisories are **not** scanned and stay waived. Tests: `scripts/tests/test_deps_audit_strict.py` (offline fixtures; no PyPI).

No package bumps in this pass — PyJWT 2.13.0 and cryptography 50.0.0 remain latest. Direct engine pins are clean. The first live `make deps-audit-strict` run (2026-08-13) **correctly fails** on three HIGH *transitive* Starlette advisories pulled by `fastapi==0.115.12` (`starlette>=0.40,<0.47`): PYSEC-2026-249 / GHSA-82w8-qh3p-5jfq, PYSEC-2026-1942 / GHSA-7f5h-v6xp-fcq8, PYSEC-2026-2281 / GHSA-wqp7-x3pw-xc5r. Clearing them requires Starlette ≥1.3.1 and therefore a FastAPI bump outside this residual slice. See residuals.

---

## Residuals (C2 stays Open)

These are honest leftovers. None of them are silently treated as closed.

| Residual | Why it remains | Owner |
|---|---|---|
| **Dep CI / GitHub Actions off** | Local `make deps-audit-strict` now fails on high/critical runtime Python pins. GitHub Actions stay globally disabled and must **not** be enabled here, so there is still no enabled CI gate. | Ops / later CI enablement |
| **Transitive Starlette HIGH** | `fastapi==0.115.12` requires `starlette<0.47`. Live strict audit fails on three HIGH Starlette GHSAs (form-limit DoS and related). Not bumped: Starlette 1.x needs a FastAPI upgrade, out of this local-code slice. | Follow-on (FastAPI/Starlette) |
| **Distributed rate limits** | In-proc limiter is Community-ok. Multi-replica / public-edge still needs Redis or LB/CDN in front. Redis is not reused for this. | Enterprise / multi-replica deploy |
| **Deploy auth hard-on** | Code default `BOSS_REQUIRE_API_AUTH=false` (local DX). k8s/goldeneye profiles already force `true`. Non-local GA is an **ops** sign-off, not a local-code gap. | Deploy |
| **Dual-control role accounting** | Panel 07: ChangeSet approvals still match `member_id` strings to skill role names (`ceo`/`cto`). Custom member ids with `role_keys` can stall. Not in this C2 local-code slice. | Follow-on |
| **Public security contact** | `engine/SECURITY.md` still lacks a private reporting channel. Required before public/OSS GA, not Community private preview. Do not invent an address. | Legal / public announce (C3) |
| **Webhook / preview secrets** | Production webhook HMAC secrets and preview TTL/unguessable-id threat-model acceptance remain deploy documentation. | Deploy |
| **Dev-only npm advisories** | Historical vitest/vite/esbuild/postcss findings are toolchain-only; they do not ship in the Next production image. Waiver stands. | Accepted |

---

## Tests to run

```bash
PYTHONPATH=.:engine \
  .venv/bin/python -m pytest \
  engine/tests/test_oidc.py \
  scripts/tests/test_deps_audit_strict.py

DATABASE_URL=postgresql+psycopg://boss:boss@localhost:5432/boss \
  PYTHONPATH=.:engine \
  .venv/bin/python -m pytest \
  apps/api/tests/test_critical_auth_and_routing.py::test_deprovisioned_scim_user_cannot_act_via_org_member_fallback \
  apps/api/tests/test_api.py::test_high_cost_endpoints_rate_limited_429_and_emits_event \
  apps/api/tests/test_api.py::test_api_key_principal_binding_reports_session_and_blocks_impersonation \
  apps/api/tests/test_api.py::test_unbound_api_key_fails_closed_when_principal_binding_is_configured
```

---

## Sign-off rule

Upgrade C2 from Open → Done only when:

1. This file’s residuals are closed **or** formally accepted for the claimed edition (Community private vs public).
2. A fail-on-high dep audit exists in the **enabled** release path (local `make` target and/or CI — Actions stay off until an explicit decision).
3. Live dogfood still shows unauthenticated 401, impersonation 403, and SCIM-deprovision 403.

Until then: **C2 is Open.**
