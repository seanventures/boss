# Panel 10 — API Contracts, Tests & Docs (GA Readiness)

**Date:** 2026-08-12  
**Panel:** API Contract / Docs / Test Evidence Expert  
**Scope:** OpenAPI ↔ product version alignment, route parity gates, test strategy sufficiency for GA, documentation accuracy risks  
**Repo evidence only** (no live re-probe in this panel). Cross-check live dogfood against `docs/operations/LIVE_QA_2026-08-11.md` and BOSS-349.

---

## Executive verdict

| Dimension | Grade | One-line |
|---|---|---|
| **OpenAPI / product version alignment (repo)** | **GREEN** | `api/openapi.yaml` `info.version: 0.11.0` matches package/Helm/CHANGELOG/`test_release_versions_are_aligned` |
| **Route parity gates** | **RED / YELLOW** | Prefixed live-QA parity exists; full-surface gate is broken/missing from Makefile & `full-validate`; CI recipe is stale |
| **Test strategy for GA** | **YELLOW** | Deep unit/integration coverage on control-plane; root `tests/` not in default `make test`; live/E2E optional & flaky relative to GA bar |
| **Docs accuracy** | **YELLOW / RED** | Generated feature catalog + CHANGELOG solid; status/planning docs still claim old versions and pre-mkdocs world |
| **Overall contracts/tests/docs GA** | **YELLOW** | Shipable as **internal / dogfood GA with contract hygiene gate**; not yet sufficient for external API consumers without full parity + docs freeze |

---

## 1. Version alignment

### 1.1 Observed product / contract versions (repo truth)

| Artifact | Version | Path |
|---|---|---|
| OpenAPI document | **0.11.0** | `api/openapi.yaml` `info.version` (explicit note: was 0.2.1) |
| Root / web packages | **0.11.0** | `package.json`, `apps/web/package.json` |
| API client package | **0.11.0** | asserted by `engine/tests/test_distribution_contract.py` |
| Helm chart / image tags | **0.11.0** | `deploy/helm/boss/` + `values.yaml` tags |
| CHANGELOG / README | **v0.11.0** | release 2026-08-09 |
| Product PRD / tech spec series | **v2.0** | `docs/product/PRD-v2.0.md`, `TECHNICAL_SPEC-v2.0.md` (product *document* version, not app semver) |
| Specpack / reference pack | **0.1.0** | reference build pack + many workflow YAMLs (historical) |

**Alignment assessment:** App release semver and OpenAPI `info.version` are **aligned at 0.11.0 in the current tree**. That closes the P2 hygiene finding recorded against **live** dogfood on 2026-08-11 (`OpenAPI … version 0.2.1`). Repo is fixed; **deploy/image skew can still reintroduce the lag** until goldeneye serves the 0.11.0 contract.

**Axes not to conflate:**

1. **App / API contract semver** (`0.11.0`) — shipping release.  
2. **PRD/spec document series** (`2.0.0`) — product requirements generation.  
3. **Specpack object versions** (`0.1.0` etc.) — workflow/object schema versions.  
4. **Edition runtime surface** (community vs paid) — same OpenAPI file documents routes that may not mount on community.

GA risk if these axes are mixed in release notes or customer docs.

### 1.2 OpenAPI surface size

- Manual contract: `api/openapi.yaml` (~14.5k lines; far larger than the ~4.4k / version `0.2.1` snapshot still quoted in planning docs).  
- Approximately **415 path items** (path keys under `paths:`).  
- Covers control plane, commercial, enterprise SCIM/SAML, ProviderOps, GitHub ops, software factory, code-tasks, delivery, sandboxes, previews, workflows, agent studio/A2A, work orchestration, factory/orchestration helpers, etc.  
- Still **hand-maintained** (not generated from FastAPI). That is intentional per docs-agent planning, but it is a permanent drift risk.

### 1.3 Schema quality signals (contract depth, not just paths)

Strengths:

- Security schemes declared at document root (`BossBearerAuth` / `BossApiKeyAuth`).  
- Many critical paths include request bodies and typed response schemas.  
- Lifecycle surfaces for factory/previews/orchestration called out as BOSS-349 work.

Weaknesses:

- Some operations still have sparse/broken description fragments (e.g. early SAML responses with `null` placeholder fields in YAML description maps).  
- Proxy / generic endpoints use empty `schema: {}` responses (preview web proxy).  
- **No automated OpenAPI schema validation** (spectral/openapi-spec-validator) found in Makefile.  
- **No request/response parity** against Pydantic models — route presence only when parity runs.

---

## 2. Route parity gates

### 2.1 Validator design (`scripts/validate_openapi_route_parity.py`)

| Property | Behavior | GA implication |
|---|---|---|
| Implementation source | **AST-walk of `apps/api/app/main.py` only** for `@app.get/post/…` and `app.api_route` | Routes mounted via `APIRouter` / `include_router` (e.g. `build_governance_router`, edition routers) are **invisible as “implemented”** |
| Contract source | `api/openapi.yaml` `paths` + HTTP methods | Full document compared unless `--prefix` filters |
| Normalization | Path params → `{}` | Correct for method+path set equality |
| Fail modes | Missing-in-OpenAPI **and** extra-in-OpenAPI both fail | Strict; good once discovery is complete |
| Prefix filter | `--prefix` repeatable | Enables slice gates (used by live-QA target) |

Composition reality in code:

```text
main.py: thousands of @app.* routes
main.py: include_router(build_governance_router(...))  # assurance, integration-registry, agent-groups, …
main.py: mount_edition_routers(app, settings.boss_edition)  # community vs paid surfaces
```

**Parity as implemented cannot truthfully certify full FastAPI surface** until the script also:

1. Walks `apps/api/app/routers/**` for `@router.*`, and/or  
2. Imports the app and reads `app.routes` at runtime (preferred for GA), and  
3. Optionally models **edition-dependent** mount sets (community OpenAPI subset vs enterprise).

### 2.2 Makefile targets

| Target | Present? | Behavior |
|---|---|---|
| `openapi-live-qa-parity` | **Yes** | Runs parity with prefixes: `/v1/code-tasks`, `/v1/delivery`, `/v1/deployment`, `/v1/deployment-targets`, `/v1/sandboxes`, `/v1/previews`, `/v1/factory`, `/v1/orchestration` |
| `openapi-validate` | **No** | Referenced by CHANGELOG and **`.github/workflows/ci.yml`**, but **not defined** in current `Makefile` |
| Included in `full-validate`? | **No** | `full-validate` = lint, deps-audit, test, build, k8s-validate, infra-validate, release-blockers, smoke — **no OpenAPI parity** |

Plane note **BOSS-349**: “openapi-live-qa-parity **79 routes OK**” — slice success, not whole-API certification.

### 2.3 CI status

`.github/workflows/ci.yml` still contains:

```yaml
- name: OpenAPI route parity
  run: make openapi-validate
```

That step would **fail on missing target** if CI were enabled. CHANGELOG for v0.11.0 states GitHub Actions remain **intentionally disabled**, so this broken gate is latent. For GA:

- Either restore `openapi-validate` as **full** parity (after fixing router discovery), or  
- Rename CI to `openapi-live-qa-parity` and accept slice-only enforcement, and  
- Wire the chosen target into `full-validate`.

Additional CI hygiene issues (out of band but relevant): workflow pins **Python 3.9** while repo engines expect **3.11+**.

### 2.4 Parity gate score for GA

| Gate | Status |
|---|---|
| Prefixed factory/delivery/sandbox/orchestration parity | **Usable** (BOSS-349 evidence) |
| Full route parity | **Not reliable** (main.py-only AST + missing Makefile target) |
| Schema / response model parity | **Absent** |
| Edition-aware contract (community vs paid) | **Absent** — OpenAPI lists paid routes; community live 404s them (ProviderOps live QA P0) |
| Live OpenAPI version probe | **Process gap** — last recorded live version was 0.2.1 |

**Panel finding:** Route parity is a **good idea half-implemented**. Treating BOSS-349 “79 routes OK” as full API contract readiness would be incorrect.

---

## 3. Test strategy sufficiency for GA

### 3.1 Inventory (approximate, from tree + test name grep)

| Suite | Location | Scale (order of magnitude) | In default `make test` / `pnpm test`? |
|---|---|---|---|
| API control-plane | `apps/api/tests/` | **~90** test modules; **hundreds** of tests (single `test_api.py` alone is a large integration surface) | **Yes** |
| Engine | `engine/tests/` | **~30** modules; **~200** tests | **Yes** |
| Worker | `apps/worker/tests/` | **4** modules | **Yes** |
| Web (Vitest) | `apps/web/tests/` | **~37** files; **~190+** `it()` cases | **Yes** (via web package) |
| Root / platform / shepherd / deploy | `tests/` | **~18** modules (fencing, shepherd, secrets, deployment health, platform standards, …) | **No** — **not** in `package.json` `"test"` path (`apps/api/tests apps/worker/tests engine/tests` only) |
| Live dogfood / agentic QA | `make qa-live`, scripts under `scripts/qa/`, artifacts under `artifacts/live-qa-*` | Manual / ops-gated | **No** (requires `BOSS_API_KEY`) |
| QA benchmarks | `make qa-benchmark` / `qa-benchmark-e2e` | Pinned BugBox / HookWatch / ApproveIt | Optional, env-gated |
| Smoke | `make smoke` → `apps.api.tests.smoke` | Narrow seed/bootstrap | In `full-validate` |

### 3.2 What the strategy does well (GA-positive)

- **State machines & money path:** budget quote/reserve/consume/release, concurrent reserve/consume, decision lifecycle, leases, deadletters, retries — heavily covered in `apps/api/tests`.  
- **Fail-closed external effects:** provider ChangeSets, GitHub merge/sync, comms outbox, skill invocation — receipt/dedupe patterns appear throughout tests.  
- **Edition surfaces:** `test_edition_routers.py` encodes community vs paid mount rules (explains live ProviderOps 404s).  
- **Assurance / governance contracts:** specpack contract tests + workflow structure tests.  
- **Client contract smoke:** `apps/web/tests/api-client.test.ts` exercises many typed client methods against expected paths.  
- **Release version alignment test:** `test_release_versions_are_aligned` pins packages/Helm/engine to `0.11.0` (but **does not assert OpenAPI `info.version`**).  
- **Feature ledger generation:** `scripts/generate_feature_docs.py` + `make docs-check` reduce pure prose drift for the catalog.

### 3.3 Gaps relative to a GA “contract + evidence” bar

1. **Default test path omits `tests/`** — deploy/shepherd/secret-backend suites can regress without `make test` failing. GA should either include them or document a second required gate (`pytest tests/`).  
2. **No mandatory full OpenAPI parity in local release path** (`full-validate`).  
3. **No contract tests that freeze OpenAPI examples** or validate responses against `openapi.yaml` schemas (schemathesis / openapi-core style).  
4. **Live E2E is evidence, not gate** — dogfood QA shows real failure modes (deadletters, image skew, edition/UI mismatch) that unit tests cannot see.  
5. **CI disabled** — institutional memory of green CI is weak; `ci.yml` itself is inconsistent with Makefile.  
6. **Performance / load / multi-tenant isolation** at GA scale not evidenced as a required suite.  
7. **Docs strictness:** `mkdocs build --strict` exists, but nav only exposes a **small slice** of `docs/` (home, features, product v2, one architecture page, two ops pages). Large operational truth lives outside the published nav.

### 3.4 Test strategy grade

**YELLOW — sufficient to protect kernel invariants in development; insufficient alone to certify external GA contract stability.**

For GA exit criteria, panel recommends a **minimum evidence pack**:

| Required evidence | Command / artifact |
|---|---|
| Unit/integration (including root `tests/`) | `make test` + `pytest tests` (or fix package.json) |
| Prefixed + full OpenAPI parity | `make openapi-live-qa-parity` + restored full parity |
| OpenAPI version pin | extend `test_release_versions_are_aligned` |
| Docs ledger check | `make docs-check` + `make docs` |
| Live smoke (dogfood) | `make qa-live` + record version from live `/openapi.json` or exported contract |
| Benchmark canary (optional but product-critical) | `make qa-benchmark-e2e` for pinned suite |

---

## 4. Documentation accuracy risks

### 4.1 Living / good sources

| Doc | Role | Accuracy posture |
|---|---|---|
| `CHANGELOG.md` | Release narrative for v0.11.0 | Strong; matches package versions |
| `README.md` | Current release `v0.11.0` | Aligned |
| `docs/features/*` | Generated from TRACEABILITY | **96 Implemented / 3 Partial** — good if TRACEABILITY is current |
| `docs/operations/LIVE_QA_2026-08-11.md` | Live dogfood truth | Accurate *as of date*; still reports OpenAPI **0.2.1** on deploy |
| `docs/operations/PRD_CRITICAL_GAPS_2026-08-12.md` | Product vs reality | Explicit DOC-STALE class — useful |
| `mkdocs.yml` | Public docs site | Present with `strict: true` |

### 4.2 Stale / high-risk sources for GA readers

| Doc | Stale claim | Risk |
|---|---|---|
| `docs/product/IMPLEMENTATION_STATUS.md` | Last updated **2026-07-26**, release target **`v0.4.0`** | Operators may treat as shipped matrix; systematically under/over-claims relative to 0.11.0 |
| `docs/planning/oss-readiness/06-documentation-agent.md` | OpenAPI **0.2.1**, **4,439 lines**, “no mkdocs” | Entire “current state” section is historical; planning doc read as product truth causes re-work |
| Live QA report | OpenAPI version **0.2.1**, 450 paths | Conflicts with repo 0.11.0 unless redeployed — **version confusion for support** |
| Feature “Implemented” catalog | Status vocabulary not the same as live dogfood readiness | Catalog can say Implemented while community dogfood cannot exercise the surface (edition, flags, credentials) |
| Reference `boss_build_spec_pack/api/openapi.yaml` | version **0.1.0** | Harmless if clearly “reference”; dangerous if linked as active contract |
| PRD v1.0/v1.1 still in tree | Older product generations | Need clear “superseded by v2.0” banner in any public nav |

### 4.3 Versioning model risk

BOSS currently has:

- **Semver product release** (0.11.0)  
- **PRD document version** (2.0)  
- **OpenAPI info.version** (should track product release; now does in repo)  
- **Feature status ledger** (Implemented/Partial/…) without edition/deploy qualifiers  

GA docs should define a **single public version story**: “BOSS 0.11.0 implements PRD v2.0 scope X with edition matrix Y.” Without that, sales/support and OpenAPI consumers will disagree.

### 4.4 Docs generation gates

- `make docs-generate` / `docs-check` only regenerates feature docs from TRACEABILITY.  
- Does **not** regenerate OpenAPI, IMPLEMENTATION_STATUS, or live QA.  
- Mkdocs nav omits most of `docs/operations` and almost all design/planning — internal truth stays git-only.

---

## 5. Cross-cutting GA risks (contracts × tests × docs)

| ID | Risk | Severity | Mitigation |
|---|---|---|---|
| CTD-01 | `make openapi-validate` missing while CI/CHANGELOG reference it | **P0 process** | Add target; include in `full-validate` |
| CTD-02 | Parity script ignores routers / edition mounts | **P0 contract** | Runtime `app.routes` discovery + edition matrix |
| CTD-03 | OpenAPI version fixed in repo but live dogfood may still serve 0.2.1 | **P1 deploy** | Redeploy; probe live contract version in `qa-live` |
| CTD-04 | OpenAPI lists paid ProviderOps routes; community UI still calls them | **P0 product** (see live QA) | Edition-aware OpenAPI export **or** edition-aware UI + contract tests |
| CTD-05 | Root `tests/` excluded from default test command | **P1 quality** | Add to `pnpm test` / Makefile |
| CTD-06 | No OpenAPI `info.version` assertion in distribution tests | **P2** | Extend `test_release_versions_are_aligned` |
| CTD-07 | IMPLEMENTATION_STATUS + planning docs claim pre-0.11 world | **P1 docs** | Rewrite or banner “historical as of …” before GA |
| CTD-08 | Hand OpenAPI without schema/response tests | **P1 contract** | Add schema lint + selective response validation |
| CTD-09 | CI disabled + stale workflow | **P1 process** | Enable fixed CI or delete false confidence |
| CTD-10 | Feature catalog “Implemented” ≠ dogfood-ready | **P1 honesty** | Add edition / live-evidence columns or link PRD gaps |

---

## 6. GA readiness recommendations (ordered)

### Must before external API GA

1. **Restore a working full parity gate** that discovers **all** mounted routes (including routers and edition mounts).  
2. **Wire parity into `full-validate`** and fix `.github/workflows/ci.yml` to call a real target.  
3. **Assert OpenAPI `info.version == 0.11.0`** (or release constant) in distribution tests.  
4. **Publish edition-scoped contract story** (community subset vs enterprise full) so 404s are specified, not accidental.  
5. **Freeze or rewrite** `IMPLEMENTATION_STATUS.md` for 0.11.0 and stop pointing newcomers at July v0.4.0 status.  
6. **Include root `tests/` in default test entrypoint.**  
7. **Redeploy dogfood** so live OpenAPI matches repo; re-run live QA and attach evidence to Plane.

### Should for sustainable contract hygiene

8. Add OpenAPI schema lint (3.1.0 validity) in CI.  
9. Contract tests for top customer paths (commands, tasks, budget, decisions, code-tasks, receipts, events).  
10. Generate or diff-check TS `packages/api-client` against OpenAPI (or reverse: forbid client paths not in OpenAPI).  
11. Extend TRACEABILITY/feature catalog with **edition + evidence** so “Implemented” is not misread as “live on community dogfood.”  
12. Keep live QA artifacts under `docs/operations/` or `artifacts/` with explicit **as-of version**.

### Nice / post-GA

13. Optionally generate OpenAPI from FastAPI for internal debug while keeping a reviewed subset as public contract.  
14. Broaden mkdocs nav to operations runbooks and GA readiness pack.  
15. Schemathesis / property-based fuzz on authenticated sandbox.

---

## 7. Evidence map (for auditors)

| Claim | Evidence |
|---|---|
| OpenAPI version 0.11.0 | `api/openapi.yaml` lines 1–5 |
| Product packages 0.11.0 | `package.json`, `apps/web/package.json`, Helm values, CHANGELOG |
| Version alignment test (no OpenAPI) | `engine/tests/test_distribution_contract.py` |
| Parity script main.py-only | `scripts/validate_openapi_route_parity.py` |
| Live-QA parity prefixes | `Makefile` target `openapi-live-qa-parity` |
| Missing `openapi-validate` | Makefile `.PHONY` / targets vs `.github/workflows/ci.yml` step |
| BOSS-349 79 routes | `docs/operations/PLANE_TRACKING_LIVE_QA.md` |
| Live OpenAPI was 0.2.1 | `docs/operations/LIVE_QA_2026-08-11.md` |
| Default tests omit `tests/` | root `package.json` `"test"` script |
| Stale status doc | `docs/product/IMPLEMENTATION_STATUS.md` header |
| Feature catalog counts | `docs/features/index.md` (96/3/0/0) |
| Router composition | `apps/api/app/main.py` `include_router` + `mount_edition_routers` |
| Governance router paths | `apps/api/app/routers/governance.py` |

---

## 8. Panel conclusion

**Contracts (repo):** OpenAPI is now **version-aligned** with product **0.11.0** and is a large, hand-curated control-plane surface. That is a material improvement over the 0.2.1 live-QA finding.

**Parity gates:** **Not GA-grade.** Slice parity (`openapi-live-qa-parity`) is valuable and reported green for 79 routes (BOSS-349), but full-route validation is architecturally incomplete (main.py AST only), **absent from `full-validate`**, and **broken as named in CI** (`openapi-validate` missing).

**Tests:** **Strong kernel/regression depth**, weak **release packaging of evidence** (root suite exclusion, disabled/stale CI, live optional). Enough for continued dogfood iteration; not enough alone for “external API GA certified.”

**Docs:** **Split brain.** Generated feature catalog + CHANGELOG track 0.11 well; status/planning/live reports still encode older versions and pre-site assumptions. **Accuracy risk is high** for anyone reading IMPLEMENTATION_STATUS or oss-readiness “current state” as of today.

**Final panel grade: YELLOW** — proceed with **internal GA / dogfood certification** only after CTD-01–CTD-04 and CTD-07 are closed or explicitly accepted with residual risk; **defer public API GA** until full parity, edition-scoped contract, and docs freeze land.

---

*Panel 10 — Contracts / Tests / Docs — BOSS GA readiness 2026-08-12*
