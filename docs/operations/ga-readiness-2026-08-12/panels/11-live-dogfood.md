# Panel 11 — Live Dogfood Functional QA

**Date:** 2026-08-12 (UTC afternoon probe window ~14:02Z)  
**Panel role:** Live behavioral checks — *working, not merely healthy*  
**Environment:** goldeneye dogfood (`boss-app`), **community** edition  
**API:** `https://boss-api.local.sean.network`  
**Web:** `https://boss.local.sean.network`  
**Project:** `boss-dogfood` / org `org_boss` / workspace `ws_boss`  
**Auth:** `BOSS_REQUIRE_API_AUTH=true`; key from `/tmp/boss_keys/BOSS_API_KEY` (never printed)  
**OpenAPI:** version `0.2.1`, **471** paths  
**Artifacts:** `artifacts/qa/live-dogfood-gate-20260812T140205Z.json`  
**Method:** live HTTP probes (Bearer + `X-Boss-Api-Key`), `make qa-live`, SSH goldeneye inventory (image/env *names* only; no secret values)

---

## Verdict (one line)

**YELLOW / control-plane dogfood is functionally live and the BOSS-351 gate is green, but deploy SHA skew (api/web `968bc47` vs main `4727de7`, worker `91faf6d`), 41 open deadletters, sandbox microVM provision failure, and loopback-only preview internals block a clean GA “working end-to-end” claim.**

| Sub-area | Result | Confidence |
|---|---|---|
| Health + auth fail-closed | **PASS** | High |
| Community ProviderOps reads | **PASS** (param-gated) | High |
| Enterprise routes absent (404) | **PASS** (expected) | High |
| GitHub connect/bindings/repos | **PASS** | High |
| Budget status + quote | **PASS** (project scope) | High |
| Decisions / deadletters visibility | **PASS** list; **WARN** backlog | High |
| Sandbox host status | **PASS** connected; **WARN** exec | High |
| Previews / loopback | **WARN** | High |
| `make qa-live` gate | **PASS** 11/11 | High |
| Image tags vs `main` HEAD | **WARN** deploy skew | High |

**Composite panel grade: YELLOW (functional control plane; not deploy-current; execution substrate incomplete).**

---

## Deploy truth (live inventory)

| Component | Image tag | Status | Notes |
|---|---|---|---|
| `boss-app-api-1` | `boss-api:968bc47` | Up ~2h, healthy | SHA of github easy-connect / env auth |
| `boss-app-web-1` | `boss-web:968bc47` | Up ~2h | Same tag as API |
| `boss-app-worker-1` | `boss-worker:91faf6d` | Up ~12h | **Older** than api/web |
| Repo `main` HEAD | `4727de7` | local + origin | OpenAPI parity merge #161 |
| Edition | `BOSS_EDITION=community` | confirmed via container env | Community dogfood |
| Auth gate | `BOSS_REQUIRE_API_AUTH` present | true | Unauth → 401 |

### Image skew analysis (check 11)

| Compare | Distance | Risk |
|---|---|---|
| Deployed api/web `968bc47` → main `4727de7` | **5 commits** | OpenAPI parity (#161), live QA route docs (#160), github easy-link merge (#158), studio starter catalog (#159) **not on dogfood API/web** |
| Worker `91faf6d` → api/web `968bc47` | **4 commits** (incl. community BYOK ProviderOps kernel, github easy-connect, hydration fix) | Worker missing community ProviderOps / easy-connect era code paths |
| Worker `91faf6d` → main `4727de7` | **9 commits** | Highest skew; worker oldest of the three |

**Verdict:** **WARN — deploy skew risk is real.** Dogfood is *not* on main HEAD. Api/web share a tag (good); worker lags both api/web and main (bad). Prior Aug-11 `view2`/`norebuild` skew is resolved to git SHAs, but triple-tag alignment is still broken.

Commits on main not in `968bc47`:

1. `729edfa` feat(studio): starter catalog setup (#159)
2. `b6f026f` Merge #158 github-project-easy-link
3. `46c53bb` docs(api): live QA route families (#160)
4. `7e98bd9` fix(openapi): BOSS-349 schema parity
5. `4727de7` Merge #161 openapi-parity

---

## Check matrix (PASS / FAIL / WARN + HTTP)

### 1. `/health`

| Check | HTTP | Result | Evidence |
|---|---|---|---|
| `GET /health` (no auth) | **200** | **PASS** | `status=ok`, `database_connected=true`, `github_configured=true`, `openai_configured=true` |

### 2. Auth with Bearer key

| Check | HTTP | Result | Evidence |
|---|---|---|---|
| `GET /v1/projects` unauthenticated | **401** | **PASS** | `Valid BOSS API key or OIDC bearer token required.` |
| `GET /v1/projects` `Authorization: Bearer <key>` | **200** | **PASS** | `items` count=1 |
| `GET /v1/projects` `X-Boss-Api-Key: <key>` | **200** | **PASS** | Same payload; gate script uses this header |
| Project `boss-dogfood` present | **200** | **PASS** | id/slug `boss-dogfood`, org `org_boss`, workspace `ws_boss` |
| Bare `Authorization: <key>` (no Bearer) | **401** | **PASS** (strict) | Scheme required |

### 3. Community: providers, secret-refs, changesets, worker-pools

> Note: Aug-11 live QA reported these as **404** under community. **2026-08-12 live shows routes mounted** (post community BYOK / ProviderOps kernel). Missing `project_id` → **422**, not 404.

| Check | HTTP | Result | Evidence |
|---|---|---|---|
| `GET /v1/provider-definitions` | **200** | **PASS** | count=**5** definitions |
| `GET /v1/providers` (no query) | **422** | **WARN** (contract) | `project_id` required — not silent drop |
| `GET /v1/providers?project_id=boss-dogfood` | **200** | **PASS** | count=**4** provider accounts |
| `GET /v1/secret-refs?project_id=boss-dogfood` | **200** | **PASS** | count=**7** refs (values not returned / not logged) |
| `GET /v1/changesets?project_id=boss-dogfood` | **200** | **PASS** | count=**1** |
| `GET /v1/worker-pools?project_id=boss-dogfood` | **200** | **PASS** | count=**0** (empty but live) |
| `GET /v1/resources?project_id=boss-dogfood` | **200** | **PASS** | count=**0** |
| `GET /v1/resource-receipts?project_id=boss-dogfood` | **200** | **PASS** | count=**1** |
| `GET /v1/provider-health?project_id=boss-dogfood` | **200** | **PASS** | count=**29** health samples |
| `GET /v1/scale-plans` | **404** | **WARN** | Still not mounted (or not community) |

**Community ProviderOps read path: PASS** for the requested surface (providers / secret-refs / changesets / worker-pools) when called with `project_id`.

### 4. account-connect / self-improvement (expect 404 community)

| Check | HTTP | Result | Evidence |
|---|---|---|---|
| `GET /v1/account-connect` | **404** | **PASS** | Expected community omission |
| `GET /v1/account-connect/status` | **404** | **PASS** | Expected |
| `GET /v1/self-improvement` | **404** | **PASS** | Expected |
| `GET /v1/self-improvement/status` | **404** | **PASS** | Expected |
| `GET /v1/account-connections` | **404** | **PASS** | Expected |
| `GET /v1/self-improvements` | **404** | **PASS** | Expected |

### 5. GitHub connections + bindings + list repos / easy-connect

| Check | HTTP | Result | Evidence |
|---|---|---|---|
| `GET /v1/github/connections?project_id=boss-dogfood` | **200** | **PASS** | **2** connections, both `status=active`, `credential_configured=true` |
| `GET /v1/github/repository-bindings?project_id=boss-dogfood` | **200** | **PASS** | **2** bindings |
| `GET /v1/github/connections/{id}/repositories?project_id=…` | **200** | **PASS** | **11** repos listed (incl. `seanheiney/boss`, `paramg/boss-sandbox`) |
| `GET /v1/github/repository-activity?project_id=…` | **200** | **PASS** | **1** activity row (30d window) |
| `GET /v1/github/easy-connect` | **405** | **PASS** (presence) | Method Not Allowed — route exists, POST-only |
| `POST /v1/github/easy-connect` empty body | **422** | **PASS** (presence) | Validation only; no connection created |
| OpenAPI `POST /v1/github/easy-connect` | present | **PASS** | Registered in live OpenAPI |
| `GET /v1/github/bindings` (legacy path) | **404** | **INFO** | Canonical path is `repository-bindings` |
| `GET /v1/github/repos` (legacy path) | **404** | **INFO** | Use connection repositories sub-resource |

Binding sample (non-secret): two project bindings active against dogfood GitHub connections (PAT auth, account_login present, required permissions granted).

### 6. Budget status / quote

| Check | HTTP | Result | Evidence |
|---|---|---|---|
| `GET /v1/budget/status?scope_type=project&scope_id=boss-dogfood` | **200** | **PASS** | `budget_state=green`; cap≈$129; available≈$88.27; reserved=$1.50; actual≈$39.23 |
| `GET /v1/budget/status?scope_type=org&scope_id=org_boss` | **400** | **WARN** | `Only project scope is supported.` — org rollup not live |
| `POST /v1/budget/quote` (project, model_api, $0.05) | **200** | **PASS** | `allowed=true`, `budget_state=green`, post-reservation still green |
| Quote without reserve | — | **PASS** | Probe did **not** create a reservation (quote-only) |

### 7. Decisions / deadletters (counts only)

| Check | HTTP | Result | Evidence |
|---|---|---|---|
| `GET /v1/decisions` | **404** | **INFO** | Collection root not mounted; use inbox |
| `GET /v1/decisions/inbox` (no project) | **422** | **WARN** (contract) | `project_id` required |
| `GET /v1/decisions/inbox?project_id=boss-dogfood` | **200** | **PASS** | **61** items — pending **6**, approved **13**, expired **36**, rejected **6** |
| `GET /v1/deadletters?project_id=boss-dogfood` | **200** | **PASS** | **41** items, **all `open`** |
| `GET /v1/deadletters?project_id=…&status=open` | **200** | **PASS** | open filter count=**41** |

**Backlog WARN:** 41 open deadletters (was 39 on 2026-08-11). Newest sampled reasons include:

| Reason bucket (sampled) | ~Count |
|---|---:|
| Claude agent max turns | 6 |
| Model 413 request too large | 5 |
| Model 402 credits/spend | 4 |
| timed out | 4 |
| Executable not allowlisted (local_subprocess) | 3 |
| Gate: no test files | 3 |
| Missing SecretRef `env:BOSS_DIGITALOCEAN_TOKEN` | 2+ (incl. **2026-08-12** resource_receipt) |
| NoneType `.get` bug | 2 |

Failures are **visible** (not silent) — hard-rule compliant — but backlog is not being reaped.

### 8. Sandboxes host status

| Check | HTTP | Result | Evidence |
|---|---|---|---|
| `GET /v1/sandboxes` | **200** | **PASS** | `sandboxes=[]`; host present |
| Host `connected` | — | **PASS** | `connected=true` |
| Host kind / capacity | — | **PASS** | kind=`Firecracker micro-VM`, capacity=**8**, available=**8**, running=**0** |
| Host URL | — | **INFO** | `http://10.100.9.1:8077` (cluster network, not public) |
| `GET /v1/sandboxes/helpers` | **200** | **WARN** | Control plane 200 but upstream helpers **401 Unauthorized** from sandbox host |
| `GET /v1/diag/sandbox-roundtrip` | **200** | **WARN** | `reached=true`, submit **202**, then `final_status=failed`, `final_phase=provisioning` (~2s) — **connectivity OK, provision/exec not OK** |

Aligns with Panel 05: host health restored; guest rootfs / provision path still broken for real tasks.

### 9. Previews — any loopback URLs?

| Check | HTTP | Result | Evidence |
|---|---|---|---|
| `GET /v1/previews` (no project) | **422** | **WARN** (contract) | `project_id` required |
| `GET /v1/previews?project_id=boss-dogfood` | **200** | **PASS** (list) | **50** previews |

**Status breakdown (50):** expired 30 · failed 10 · stopped 7 · verified 3 · **0 running/active**

**Loopback analysis:**

| Field | Observation | Result |
|---|---|---|
| `internal_url` | Widespread `http://127.0.0.1:<ephemeral-port>` (35+ distinct ports in sample) | **WARN** |
| `open_url` / `url` | Proxy path `/v1/previews/{id}/web/` (not raw loopback) | **PASS** design |
| Live operator “View” | No active previews to click-test this window | **WARN** (cannot prove publicize live) |

**Interpretation:** Storing loopback as `internal_url` is expected for process/preview_host substrate. Operator-facing path is the API proxy. **Risk remains** if UI ever surfaces `internal_url` instead of `open_url`, or if proxy/publicize is absent on older web builds. With api/web on `968bc47` (post easy-connect; may predate some publicize polish on main), treat as **WARN** until an active preview is opened end-to-end.

### 10. `make qa-live` / `scripts/qa/live_dogfood_gate.py`

| Check | Result | Evidence |
|---|---|---|
| `BOSS_API_KEY` set from key file | **PASS** | length=64 (not printed) |
| `make qa-live` exit code | **0** | overall=**PASS** |
| Required checks | **10/10** | all PASS |
| Optional checks | **1/1** | metatron PASS |
| Report path | written | `artifacts/qa/live-dogfood-gate-20260812T140205Z.json` |

Gate line results:

| Gate check | HTTP | Result | Detail |
|---|---|---|---|
| health | 200 | PASS | ok + DB |
| auth.unauthenticated_rejected | 401 | PASS | fail-closed |
| auth.projects | 200 | PASS | boss-dogfood present |
| providers.definitions | 200 | PASS | count=5 |
| providers.list | 200 | PASS | count=4 |
| budget.quote | 200 | PASS | allowed=true, green |
| command.propose | 201 | PASS | task accepted (`tsk_65d66ff3…`) |
| command.task_exists | 200 | PASS | durable task visible |
| decisions.inbox | 200 | PASS | count=60 at gate time (61 later) |
| deadletters.list | 200 | PASS | count=41 |
| metatron.query | 200 | PASS | budget-aware answer (~258ms) |

**Side effect:** gate proposed a low-stakes command task tagged `[BOSS-351 live-gate]` (intake only; no PR). Visible in task list by design.

### 11. Image tags vs main HEAD (`968bc47` vs `4727de7`)

| Check | Result | Detail |
|---|---|---|
| main HEAD | `4727de7` | Merge #161 openapi parity |
| Live api tag | `968bc47` | **behind main by 5 commits** |
| Live web tag | `968bc47` | matches api |
| Live worker tag | `91faf6d` | **behind api/web and main** |
| `968bc47` is ancestor of main | yes | safe fast-forward path |
| Triple-service tag equality | **FAIL** | worker ≠ api/web |
| Parity with main HEAD | **FAIL** | none of the three services on `4727de7` |

**Deploy skew risk: WARN (elevated).** Recommend redeploy **api + web + worker** to the same main SHA (or a single release tag) before claiming GA dogfood = main.

---

## Adjacent control-plane smoke (supporting)

| Check | HTTP | Result | Notes |
|---|---|---|---|
| `GET /v1/tasks?project_id=boss-dogfood&limit=5` | 200 | PASS | list live |
| `GET /v1/jobs?project_id=boss-dogfood&limit=20` | 200 | PASS | list live |
| `GET /v1/events?project_id=boss-dogfood&limit=5` | 200 | PASS | audit trail live |
| `GET /v1/receipts?project_id=boss-dogfood&limit=5` | 200 | PASS | receipts live |
| `GET /openapi.json` | 200 | PASS | 471 paths, v0.2.1 |
| `GET https://boss.local.sean.network/` | 200 | PASS | org gate HTML ~7 KB |
| `GET https://boss.local.sean.network/?org=org_boss` | 200 | PASS | Mission Control SSR ~6.6 MB |
| `GET https://boss.local.sean.network/health` | 404 | WARN | no web `/health` route (API has `/health`) |

---

## Ranked findings

### P0 — none new for “API reachable / auth / budget / command intake”

Control-plane vertical slice required by BOSS-351 gate is **green**.

### P1 — Deploy SHA skew (api/web/worker/main)

- Worker on `91faf6d` while api/web on `968bc47` while main is `4727de7`.
- Risk: worker-side behavior for ProviderOps / github easy-connect / OpenAPI-era fixes diverges from API.
- **Action:** single-tag redeploy of all three services to main (or release cut).

### P1 — Deadletter backlog (41 open, +2 vs Aug-11)

- All status=`open`; includes **same-day** DigitalOcean SecretRef miss (resource plane path is live enough to fail closed — good — but ops hygiene lagging).
- **Action:** triage/requeue/resolve; fix missing DO token SecretRef or disable DO skill probes on community dogfood.

### P1 — Sandbox provision still failing

- Host connected + capacity advertised, but `diag/sandbox-roundtrip` fails in `provisioning`; helpers 401 from host.
- Blocks true factory E2E (code-task → microVM → PR).

### P2 — Preview loopback internals + no active previews

- `internal_url` is loopback; proxy `open_url` exists; no running preview to validate View button live this session.

### P2 — Contract UX: many list routes 422 without `project_id`

- Fail-closed is correct; Mission Control must always pass `project_id` (edition-aware). Bare `/v1/providers` etc. will look “broken” if UI omits the query.

### P2 — Org budget scope unsupported

- Project budget works; org-level status returns 400.

### P3 — Web has no `/health`

- API health is the canary; web 404 on `/health` is cosmetic unless LB expects it.

---

## Hard-rules spot check (live)

| Rule | Live signal | OK? |
|---|---|---|
| No silent drops | Deadletters + decisions visible; 422 on bad input | **Yes** |
| Auth required for `/v1/*` | 401 without key | **Yes** |
| Budget before spendy path | Quote allowed; gate did not reserve without need | **Yes** (quote-only probe) |
| Receipts exist | `/v1/receipts` 200; resource_receipts count≥1 | **Yes** |
| Community vs enterprise boundary | account-connect / self-improvement 404 | **Yes** |
| Secrets not in report | Key never printed; SecretRef values not dumped | **Yes** |

---

## Comparison to 2026-08-11 live QA

| Area | 2026-08-11 | 2026-08-12 (this panel) |
|---|---|---|
| ProviderOps routes on community | **404** (broken UI fan-out) | **200** with `project_id` (**fixed/regressed-forward**) |
| Image tags | api/web `view2`, worker `norebuild` | api/web `968bc47`, worker `91faf6d` |
| Deadletters open | 39 | **41** |
| Decisions inbox | 54 | **61** (6 pending) |
| Budget | green | green |
| `make qa-live` | not recorded same way | **PASS 11/11** |
| Sandbox host | issues / skew | connected; provision still fail |

---

## GA readiness call (this panel only)

| Question | Answer |
|---|---|
| Is dogfood **reachable and authenticated**? | **Yes** |
| Is the **control-plane vertical slice working** (health, auth, providers, budget, command→task, decisions, deadletters, metatron)? | **Yes** (`make qa-live` green) |
| Is dogfood **deployed at main HEAD**? | **No** — WARN skew |
| Is **execution substrate** (microVM task success) proven? | **No** — provision fail |
| Are **previews operator-reachable** with zero loopback footguns? | **Partial** — proxy paths yes; loopback internals yes; no live active preview |
| Community edition boundary respected? | **Yes** for account-connect / self-improvement |

**Panel recommendation:** treat live dogfood control plane as **demo-ready for intake/budget/github-connect**, **not** as proof of full factory GA until (1) three-way image pin to main, (2) deadletter burn-down, (3) sandbox provision green, (4) one active preview View verified.

---

## Reproduction (operators)

```bash
# key must already exist; never commit or echo it
export BOSS_API_KEY="$(cat /tmp/boss_keys/BOSS_API_KEY)"
export BOSS_API_URL="https://boss-api.local.sean.network"
export BOSS_PROJECT_ID="boss-dogfood"

make qa-live
# → artifacts/qa/live-dogfood-gate-*.json

curl -sS "$BOSS_API_URL/health"
curl -sS -H "Authorization: Bearer $BOSS_API_KEY" \
  "$BOSS_API_URL/v1/providers?project_id=$BOSS_PROJECT_ID" | jq '.items | length'
```

SSH inventory (names only): `docker ps` filter `boss-app`; confirm `BOSS_*_IMAGE` and `BOSS_EDITION` names/tags — do not dump secret values.

---

## Secrets / safety

- API key material: **not** written to this report, gate JSON summary, or shell history intentionally.
- SecretRef **values** not retrieved; only counts/metadata.
- No provider write / merge / deploy / real customer send performed beyond gate’s documented low-stakes command propose + budget quote.

---

*Panel 11 complete — Live Dogfood Functional QA Expert — 2026-08-12.*
