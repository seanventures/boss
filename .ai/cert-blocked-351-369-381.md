# HOLD certification — BOSS-351 / 369 / 381 / 300 / 337 / 314 / 293

**Certified:** 2026-08-15T13:56Z  
**Verdict:** **HOLD** for every ticket below. **Do not close. Do not stamp GA.**  
**Live:** `https://boss-api.local.sean.network` · project `boss-dogfood` · edition `community`  
**Shared tag:** `b6d8ccc` (`boss-api` + `boss-web` + `boss-worker`)  
**Auth:** `X-Boss-Api-Key` from live `/srv/apps/boss-app/.env` (never printed)  
**Plane:** workspace `goldeneye` / project `cc97d5cc-e8c6-4893-ac14-7f646c37f3d7`  
**Plane key:** fetched from ops store `GET $OPS_BASE_URL/secret?key=PLANE_API_KEY` (present; value not printed)  
**This probe:** GET-only. **No spend.** Flags **not** enabled. No `POST /v1/code-tasks`. No official QA POST. No easy-connect write-through. No license invented.

Independent re-probe of `.ai/sealed-live-blocker.md`, `.ai/byok-live-blocker.md`, `.ai/official-qa-live.md`. Those notes remain true.

---

## Shared live facts (this session)

| Probe | Result |
|---|---|
| `ssh root@goldeneye 'grep -c BOSS_FACTORY_ALLOW_CREATE /srv/apps/boss-app/.env'` | **`0`** (key **absent**) |
| `grep -c '^BOSS_FACTORY_ALLOW_CREATE='` | **`0`** |
| `grep -c '^BOSS_QA_ALLOW_CANARIES='` | **`0`** |
| `grep -c '^BOSS_QA_ALLOW_OFFICIAL='` | **`0`** |
| `grep -c '^BOSS_KMS_MASTER_KEY='` / `_FILE=` / `_ALLOW_EPHEMERAL_MASTER=` | **`0` / `0` / `0`** |
| `GET /health` | **200** `status=ok` · DB / GitHub / OpenAI configured |
| `GET /v1/auth/session` | **200** `auth_required=true` `auth_method=api_key` |
| `GET /v1/edition?project_id=boss-dogfood` | **200** `edition_id=community` · `license_candidate=AGPLv3` · **`legal_review_required=true`** |
| `GET /v1/kms/health` | **200** · **`local_vault.ok=false`** |
| `GET /v1/qa/benchmark-apps?project_id=boss-dogfood` | **200** · official catalog reports **missing / not_scored** |
| `GET /v1/factory/wip?project_id=boss-dogfood&wip_limit=5` | **200** `admit=true` · `live_pr_success.proven=false` |

`GET /v1/kms/health` `local_vault` error (verbatim):

> `local_vault requires BOSS_KMS_MASTER_KEY or BOSS_KMS_MASTER_KEY_FILE (or BOSS_KMS_ALLOW_EPHEMERAL_MASTER=true for tests only)`

Other KMS backends this probe: `vault`/`openbao` not configured; `platform_secret_store` not configured; `file` ok; `environment` ok (read-only). Unauthenticated `/v1/kms/health` → **401** (fail-closed).

---

## BOSS-351 — sealed factory E2E — **HOLD**

| Field | Value |
|---|---|
| Plane | `c1f3b9c6-923e-4b6e-b211-939934f908c5` · **In Progress** |
| Title | P0: One sealed dogfood E2E — CEO command → budgeted code evidence → PR decision → receipts |
| **Named blocker** | **`BOSS_FACTORY_ALLOW_CREATE` absent (OFF)** |
| Close? | **No. Do not close.** |

**Why HOLD.** A sealed weekly proof requires `simulated=false` + `open_pr=true` governed `code.task`, then a `github.pull_request.create` receipt + hold decision that pass `validate_sealed_receipt` / `validate_sealed_decision`. The factory gate refuses a budgeted create unless `BOSS_FACTORY_ALLOW_CREATE=1`. Live host `.env` does not contain that key (`grep -c` → 0). `BOSS_QA_ALLOW_CANARIES` is also absent (secondary; live PR path fail-closes without it).

This session did **not** start a sealed run. Starting one would spend (model + `github.pull_request.create`). Flags were not enabled.

**Also not a sealed proof (present and healthy, not sufficient):**

- WIP `admit=true` (0 in flight / 5 slots)
- GitHub plane `mission_control_binding` `seanheiney/boss`
- `live_pr_success` = `39/40 from existing receipts (not a sealed weekly proof)` · **`proven=false`**
- `fixture_supported=true` (simulated fixture only)
- `coding_substrate=local` (configured microvm; claims frozen)

No new `receipt_id` + hold `decision_id` pair exists for a weekly dogfood run. Historical PR receipts are explicitly labeled **not a sealed weekly proof**.

**Unblock (operator only — not done here):** set `BOSS_FACTORY_ALLOW_CREATE=1` **and** `BOSS_QA_ALLOW_CANARIES=1` for one canary (or written spend approval), then one narrow `POST /v1/code-tasks` with `simulated=false`, `open_pr=true`, no merge. Record real receipt + hold ids. Do not invent them.

---

## BOSS-369 — BYOK bind — **HOLD**

| Field | Value |
|---|---|
| Plane | `a8cf92b4-77ab-4298-be98-1ed79a1180a2` · **In Progress** |
| Title | Community BYOK paste-and-connect (model + infra keys) |
| **Named blocker** | **vault master missing** (`local_vault.ok=false`) |
| Close? | **No. Do not close.** |

**Why HOLD.** Write-through to `local_vault` cannot complete. Live `GET /v1/kms/health` reports `local_vault.ok=false` with the master-key error above. Host `.env` has **no** `BOSS_KMS_MASTER_KEY`, `BOSS_KMS_MASTER_KEY_FILE`, or `BOSS_KMS_ALLOW_EPHEMERAL_MASTER`. Enabling a master would be new host config. Not done.

**What is bound (not a local_vault bind):**

| Surface | Live |
|---|---|
| Providers | 4 accounts · **0** `local_vault:` pointers |
| Secret refs | 7 · all `storage_backend=environment` · **`last4` absent** · **0** vault refs |
| OpenAI | `prov_78c91c3fca5d47a8846a` · `dogfood-openai-byok` · `degraded` · `credential_refs.primary=env:OPENAI_API_KEY` · `secret_7c6bc09399b24e77a58b` |
| Anthropic | no provider account |

No `secret_ref_id` from a local_vault write-through. No openai/anthropic `last4`. Prior easy-connect dummy probe (documented in `.ai/byok-live-blocker.md`) returned **503** on vault-master; this session did **not** re-POST.

**Unblock (operator only — not done here):** set a durable `BOSS_KMS_MASTER_KEY` or `BOSS_KMS_MASTER_KEY_FILE` on api+worker, recreate on the same shared tag **without** `--env-template`, re-probe until `local_vault.ok=true`, then one easy-connect write-through (`read_only=true`, `test_after=false`). Confirm `secret_ref_id` + `last4` only. Do not invent a bind.

---

## BOSS-381 — official QA POST — **HOLD**

| Field | Value |
|---|---|
| Plane | `79f85c21-40e7-45ff-8751-c9cedf41c2a1` · **In Progress** |
| Title | Official QA /reports for BugBox HookWatch ApproveIt (scorecard v1.1) |
| **Named blocker** | **`BOSS_QA_ALLOW_OFFICIAL` unset** (and official catalog reports **missing / not_scored**) |
| Close? | **No. Do not close.** |

**Why HOLD.** `scripts/qa/official_reports.py` will not POST spendy official runs unless `BOSS_QA_ALLOW_OFFICIAL=1`. Live host `.env` does not contain that key. This session did **not** `POST /v1/qa/benchmark-apps/{id}/runs`.

**Catalog vs official `/report` (GET `/v1/qa/benchmark-apps?project_id=boss-dogfood`):**

| App | Catalog `built` | Official `/report` | ui / ux / capability |
|---|---|---|---|
| BugBox | **missing** | no catalog report | n/a |
| HookWatch | `crun_911888eefbd14166bce6` (`reportable=true`, `matched_on=launched_as_benchmark`) | **404** (not a QA benchmark run) | n/a — advertised report **missing** |
| ApproveIt | **missing** | no catalog report | n/a |

HookWatch catalog `built` is **not** an official score. `/v1/qa/benchmark-apps/runs/crun_911888eefbd14166bce6/report` → **404**.

Sample existing QA-benchmark `/report`s (not catalog-built, **not** official certified passes):

| run_id | HTTP | passed | ui | ux | capability |
|---|---|---|---|---|---|
| `crun_9acc1e5f44e0464099be` | 200 | **false** | **not_scored** | **not_scored** | **not_scored** |
| `crun_907e7ca9e6f54ba6945f` | 200 | **false** | **not_scored** | **not_scored** | **not_scored** |

`not_scored` stays `not_scored`. No invented `status=scored` + `score=null`. No official BugBox / HookWatch / ApproveIt certified pass.

**Unblock (operator only — not done here):** already-on `BOSS_QA_ALLOW_OFFICIAL=1` plus budget/factory path, then official POSTs that attach UI/UX evidence. Do not invent scores. Catalog `built` without a `/report` is not success.

---

## BOSS-300 — legal / OSS licensing — **HOLD**

| Field | Value |
|---|---|
| Plane | `1f573439-a75d-4556-8969-39a3c902c2b8` · **In Progress** |
| Title | GAP: resolve OSS licensing strategy decision (blocker for public flip) |
| **Named blocker** | **human legal + product licensing decision UNDECIDED** |
| Close? | **No. Do not close.** Agents must not Done this ticket. |

**Why HOLD.** Canonical gate files remain undecided:

- `docs/operations/OSS_LICENSING_DECISION.md` — **Status: UNDECIDED**
- `docs/planning/oss-readiness/DECISION-oss-licensing-strategy.md` — **Status: OPEN**
- Live edition: `license_candidate=AGPLv3` **and** `legal_review_required=true`

Engine Apache-2.0 files and a live `license_candidate` string are **not** a decision. Options A/B/C are listed, not chosen. This session did not change `LICENSE`, invent SPDX, or flip visibility.

**Unblock:** human legal counsel + product owner records `Status: DECIDED` with option, date, and signer in `OSS_LICENSING_DECISION.md`, then a human may move Plane. Not an agent action.

---

## BOSS-337 — live QA / Community GA umbrella — **HOLD**

| Field | Value |
|---|---|
| Plane | `30353bbc-3353-4837-9d13-db9529dbc70c` · **In Progress** |
| Title | BOSS deep agentic production QA (goldeneye live) — 2026-08-11 |
| **Named blocker** | **umbrella residuals still open:** BOSS-351 sealed E2E + BOSS-369 BYOK bind + BOSS-381 official QA + **14-day qa-live Day 1/14** + **C2 security NOT DONE** |
| Close? | **No. Do not close.** Not public / C1–C4 GA. |

**Why HOLD.** Umbrella cannot exit while required children and Phase C gates are HOLD:

- Sealed weekly command→PR→Decision (BOSS-351) **unproven**
- Live BYOK local_vault bind (BOSS-369) **not bound**
- Official QA scores (BOSS-381) **not posted / not_scored**
- `docs/operations/QA_LIVE_14DAY.md` calendar is **Day 1/14** (2026-08-13). Same-day re-runs do not add a day. Do not backfill.
- `docs/operations/SECURITY_C2_STATUS.md` — **C2 verdict: NOT DONE**

Working-software Views and `qa-live` 10/10 on a tag are **not** Community public GA. FEATURES stays ◐.

---

## BOSS-314 — GA readiness closure loop — **HOLD**

| Field | Value |
|---|---|
| Plane | `82d3c340-571c-474b-b62f-c21d119a60e3` · **In Progress** |
| Title | GA readiness closure loop — release baseline and evidence |
| **Named blocker** | **Phase A / Community GA evidence incomplete** — same live holds as 337 (351 / 369 / 381) plus 14-day calendar + C2 + BOSS-300 if any public announce |
| Close? | **No. Do not close.** |

**Why HOLD.** Latest Plane comment (2026-08-13) already says not GA until 14-day `qa-live` + factory reliability. This probe reconfirms factory sealed path is still flag-blocked, official scores are missing, BYOK vault is down, and the 14-day table is still Day 1/14. Closed children this cycle do not exit the umbrella.

---

## BOSS-293 — public-flip umbrella — **HOLD**

| Field | Value |
|---|---|
| Plane | `35ec645f-b01a-40ed-9930-639aa513b770` · **In Progress** |
| Title | Public-flip readiness checklist for engine repo |
| **Named blocker** | **PF-1 / BOSS-300 license UNDECIDED** (hard). Also open: PF-2 SECURITY.md placeholder, PF-3 full-history secret scan, PF-4 history rewrite review, PF-6 14-day if bundled with GA language, PF-8 live extract SoT, PF-9 partner notice |
| Close? | **No. Do not close.** Do not flip visibility. |

**Why HOLD.** `docs/operations/PUBLIC_FLIP_CHECKLIST.md` verdict remains **NOT READY**. Exit 293 only when every blocker row is Done **and** BOSS-300 is `DECIDED` in the decision doc (not inferred from code). `engine/COMMUNITY_READINESS.md` hard gates are unchecked. `make public-flip-check` is specified to fail-closed while license is UNDECIDED.

---

## What this certification is not

- Not a close recommendation for any of the seven tickets
- Not a GA stamp (Community, C1–C4, or public)
- Not a sealed receipt, local_vault bind, official score, or license choice
- Not authorization to set `BOSS_FACTORY_ALLOW_CREATE`, `BOSS_QA_ALLOW_CANARIES`, `BOSS_QA_ALLOW_OFFICIAL`, or any KMS master flag

Plane states left **In Progress**. Evidence file: this path.
