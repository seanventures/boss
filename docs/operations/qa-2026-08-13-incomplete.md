# QA Agent 3 — unfinished Community GA inventory + builds

**Date:** 2026-08-13  
**Branch:** `ai/grok/community-ga`  
**Edition:** Community  
**Plane:** BOSS-337 (umbrella); residual children 341 / 343 / 347  
**Do not claim:** 14-day calendar, live LLM factory spend, hosted Account Connect

---

## Inventory (searched, then ranked)

Searched TODO/FIXME/NotImplemented, 501 stubs, UI handlers, edition holes, Mission Control fetch paths, list endpoints, preview View, factory fixture, Studio seed.

| Item | Grade | Disposition |
|---|---|---|
| Agent Studio seed-if-empty CTA | **Done already** | `installStarterCatalog` + first-run banner call `POST /v1/agent-studio/catalog/seed`. Do not rebuild. |
| Factory simulated fixture + `fixture_supported` | **Done already (API)** | Gate refuses POST unless WIP advertises support. Live image may lag — **deploy note**. |
| Preview list `open_url` public-base | **Done already (API)** | Absolute gateway URL when `BOSS_PREVIEW_PUBLIC_BASE` set. |
| Studio empty without seed CTA | Not a hole | Wired. |
| **Factory WIP card swallows fetch errors** | **HOLE** | `fetchWipStatus` returned `null`; card hid the failure. **Built.** |
| **Factory dual-plane honesty on WIP** | **HOLE (A6)** | Binding path existed on propose; GET `/v1/factory/wip` did not advertise plane. **Built.** |
| **Dashboard card swallows fetch errors** | **HOLE** | `fetchDashboard` returned `null` with no reason. **Built.** |
| **Preview list swallows fetch errors** | **HOLE** | `fetchPreviews` returned `[]`. **Built.** |
| **View for verified-test** | **HOLE + doc** | UI only offered View for `status=running`. Verified-test with no `open_url` has **no View** (would 409). **Built honesty; documented.** |
| **List endpoints missing `project_id` defaults** | **HOLE** | `GET /v1/tasks` (and siblings) 422 without query. Community single-tenant default. **Built.** |
| **Settings / token-efficiency 500 on schema drift** | **Landed in parallel** | `519ba85` — missing tables 503 + `ensure_schema` column-reconcile-before-apply. Not rebuilt here. |
| Factory live LLM / sealed PR | Out of scope | No-spend fixture only. |
| 14-day qa-live calendar | Out of scope | Already started separately. |
| SSO / IdP packs | Community (do not rebuild paid AC) | Mounted; leave hosted Account Connect paid. |
| Enterprise clusters / marketplace / SIM automation | Paid | Hidden/badge; do not open on Community. |

### Preview View — verified-test (document)

Live list (2026-08-13): 3 **verified** / **0 running**.  
`publicize_preview` only sets `open_url` when a bind (`internal_url`) exists or status is `running`.

| verified-test row | View |
|---|---|
| Has `open_url` (public-base gateway) | Show **View verified preview** |
| No bind / no `open_url` | **No View link** — copy: `verified-test — no operator URL (not running; View would 409)` |
| Expired / stopped | No invented gateway path |

**Forbidden claim:** “Preview View opens a live app” until a **running** preview returns HTML 200.

### Factory fixture — live-ready deploy note

`GET /v1/factory/wip` now always returns `fixture_supported: true` **on this branch**.  
If the live image predates `2d1bd29` / this commit, the gate **SKIPs** `--create-fixture` (correct fail-closed). Redeploy shared-tag to advertise the flag.

---

## What we built

### 1. Factory WIP card + dual-plane honesty (A6)

- `SoftwareFactoryService.describe_github_plane` — **read-only** (does not rebind adapters / resolve SecretRefs).
- `GET /v1/factory/wip` adds `github_plane`, binding/connection ids, source, full name, keeps `fixture_supported`.
- `fetchWipStatus` never returns silent `null`; `FactoryScaleCard` shows API error + which GitHub plane (MC binding vs settings token).

### 2. Preview View honesty + dashboard / preview list errors

- `operatorPreviewOpenHref` — never invents View for verified-test without a URL; never uses loopback.
- Code Tasks shows View only when an operator URL exists; verified-without-URL is explicit.
- `fetchPreviews` / `fetchDashboard` return `apiReachable` + `blockedReason`.

### 3. List `project_id` defaults

- `resolve_list_project_id` → `BOSS_DEFAULT_PROJECT_ID` (`boss-dogfood`) for:
  `/v1/tasks`, `/v1/jobs`, `/v1/events`, `/v1/previews`, `/v1/code-tasks`, `/v1/factory/wip`, `/v1/dashboard`, `/v1/members`.
- Missing default still **422** (fail closed). Explicit `project_id` still wins (tenant scope preserved).
- Settings 500s (token-efficiency / missing relations) already shipped in `519ba85` — do not rebuild.

No secrets in new responses.

---

## Tests

- `engine/tests/test_github_dual_plane.py` — describe is read-only
- `apps/api/tests/test_qa_incomplete_surfaces.py` — defaults + WIP plane/fixture
- `apps/web/tests/code-tasks.test.ts` — View href / verified-test / loopback
- `apps/web/tests/factory.test.ts` — WIP error not swallowed
- `apps/web/tests/dashboard.test.ts` — dashboard error not swallowed

---

## Not built (still open, honest)

- Shared-tag live redeploy so factory fixture + this WIP plane land on dogfood
- Browser HTML 200 for a **running** preview (0 running on live)
- Sealed weekly command → PR (live LLM / substrate)
- Residual hydration #418
- Studio “workforce out of the box” marketing claim
