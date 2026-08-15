# Cert — BOSS-379 (activity log) + BOSS-341 (preview View URLs)

**Probed:** 2026-08-15T13:59:42Z  
**API:** `https://boss-api.local.sean.network`  
**Project:** `boss-dogfood`  
**Auth:** `X-Boss-Api-Key` from live host `/srv/apps/boss-app/.env` (`BOSS_API_KEY` present, length 64; value never printed).  
**Session:** `GET /v1/auth/session` → **200** `auth_required=true` `auth_method=api_key`. Unauthenticated GETs → **401**.  
**Health:** `GET /health` → **200** `status=ok` `database_connected=true`.  
**Live images (goldeneye compose, this probe):** shared tag **`3af9c64`** (`boss-api` / `boss-web` / `boss-worker`).  
**Host:** `BOSS_PREVIEW_PUBLIC_BASE=https://boss-api.local.sean.network` `BOSS_REQUIRE_API_AUTH=true`.  
**Plane (read-only this cert):** workspace `goldeneye`, project `cc97d5cc-e8c6-4893-ac14-7f646c37f3d7`.  
**Not GA. No spend. No official POST. No invented scores.**

## Verdicts

| Ticket | Plane id | Plane state now | This cert | CLOSE recommendation |
|---|---|---|---|---|
| **BOSS-379** activity log | `2210a144-3871-4d19-b568-4c4fec358045` | **In Progress** | **PASS** | **CLOSE** |
| **BOSS-341** preview View URLs not `127.0.0.1` | `c24f1311-fd05-451e-9a21-e6aac5444daf` | **In Progress** | **PASS** (0 leaked operator urls) | **CLOSE** the P0 loopback ticket |

**Leaked operator ids:** **none.**

## CLOSE recommendation (exact)

1. **BOSS-379 — CLOSE.** Live `GET /v1/activity?project_id=boss-dogfood&limit=5` is **200** with a real union payload (`items` + `total=44144`), classified rows, redaction, 401 unauth, and matching noconftest contracts. Router + service + UI exist. This is not an invented feed.
2. **BOSS-341 — CLOSE** the titled P0 (“Preview View URLs expose 127.0.0.1”). Live `GET /v1/previews?project_id=boss-dogfood` (50 rows) has **0** `url`/`open_url` containing `127.0.0.1` or `localhost`. Operator urls that exist are absolute `{public-base}/v1/previews/{id}/web/`. `internal_url` loopback is present on bound rows and is allowed.
3. Do **not** stamp Community GA. FEATURES activity row stays ◐ / **Not GA**. Phase B2 “new-build View HTML” is a different residual: this 50-row list had **0 running** previews, so this cert did **not** re-open a live View page in a browser.

---

## 1. Source read (this cert)

| Artifact | Path | What it does |
|---|---|---|
| Activity service | `engine/boss_engine/services/activity_log.py` | Read-only union of `events` + process steps + jobs + deadletters + receipts. Classifies from names/types only; never invents `ok`. Redacts `token`/`secret`/`password`/`api_key`/`authorization` and runs `redact()` on remaining strings. Missing optional tables → skip, not 500. |
| Activity router | `apps/api/app/routers/activity.py` | `GET /v1/activity`. Honest 503 if service missing. Sort `created_at\|status\|kind`. Envelope `{project_id, items, limit, offset}` (+ service `total`). No POST. |
| Activity UI | `apps/web/app/activity-log-ui.tsx` + `activity-log-actions.ts` + `apps/web/lib/activity-log.ts` | `data-testid="activity-log"`. Search, status/kind/actor filters, sortable columns, click row → redacted detail. Unavailable/empty copy does not invent events. `mapStatus` unknown → `info`, never `ok`. |
| Preview publicize | `engine/boss_engine/services/preview_service.py` `publicize_preview` | Copies bind to `internal_url`. Sets operator `url`/`open_url` to gateway path or `{BOSS_PREVIEW_PUBLIC_BASE}/v1/previews/{id}/web/`. Rejects loopback public base (fail closed to relative path). |
| Sweep loopback check | `scripts/qa/feature_sweep.py` `loopback_preview_open_urls` | On `factory.previews` 2xx, fails if `url` or `open_url` contains `127.0.0.1` or `localhost`. Does **not** treat `internal_url` as a leak. |
| Console View href | `apps/web/lib/code-tasks.ts` `operatorPreviewOpenHref` | Refuses loopback candidates; expired/stopped/failed → no Open href. |

---

## 2. Noconftest tests (real run)

Command (venv 3.11, `PYTHONPATH=.:engine`):

```
.venv/bin/python -m pytest --noconftest -q \
  engine/tests/test_preview_urls_are_operator_reachable.py \
  engine/tests/test_activity_log.py \
  engine/tests/test_activity_classify.py \
  scripts/tests/test_feature_sweep.py
```

| File | Result |
|---|---|
| `engine/tests/test_preview_urls_are_operator_reachable.py` | **12 passed** |
| `engine/tests/test_activity_log.py` | **7 passed** |
| `engine/tests/test_activity_classify.py` | **12 passed** |
| `scripts/tests/test_feature_sweep.py` (includes loopback) | **22 passed** |
| **Total this command** | **53 passed / 0 failed** in 1.05s |
| Loopback-only subset (`-k "loopback or preview"`) | **2 passed**, 20 deselected |

`apps/api/tests/test_activity_api.py` was **not** run: those tests need a live DB `client`/`engine` fixture (`--noconftest` would not supply them). Web `apps/web/tests/activity-log.test.ts` was **not** run (not pytest).

---

## 3. Live HTTP (this probe)

| Method | Path | HTTP | Notes |
|---|---|---|---|
| GET | `/health` | **200** | `status=ok`, `database_connected=true` |
| GET | `/v1/auth/session` | **200** | `auth_method=api_key` |
| GET | `/v1/activity?project_id=boss-dogfood&limit=5` **no key** | **401** | fail-closed |
| GET | `/v1/previews?project_id=boss-dogfood` **no key** | **401** | fail-closed |
| GET | `/v1/activity?project_id=boss-dogfood&limit=5` | **200** | see §4 |
| GET | `/v1/previews?project_id=boss-dogfood` | **200** | default list = 50 rows; see §5 |
| GET | `/v1/previews?project_id=boss-dogfood&limit=50` | **200** | same 50; same leak result |

---

## 4. BOSS-379 live evidence

Envelope keys: `items`, `limit`, `offset`, `project_id`, `total`.

| Field | Observed |
|---|---|
| `project_id` | `boss-dogfood` |
| `limit` | 5 |
| `offset` | 0 |
| `total` | **44144** |
| `items` returned | **5** |
| Secret-looking regex hits (`AKIA…`, `sk-/ghp-/xox…`, `Bearer …`) | **[]** |
| Drop-key names in JSON (`token`/`secret`/`password`/`api_key`/`authorization`) | **[]** |

Returned rows (newest first; data keys only, no payloads):

| id | source | kind | status | title | actor | entity |
|---|---|---|---|---|---|---|
| `evt_891d44514c76407da842` | event | `process_manager.ticked` | info | Process Manager Ticked | executive_console | process_manager · `pmgr_bd572e7f7a1a42a9a716` |
| `evt_417ea440e2dc4e6c85ac` | event | `receipt.created` | info | Receipt Created | system | receipt · `rcpt_26d6fba7ea5d41608c4c` |
| `rcpt_26d6fba7ea5d41608c4c` | receipt | `process_manager.tick` | info | Process Manager Tick | system | receipt · same |
| `evt_335e90de810a4494b6c9` | event | `metatron.overwatch.completed` | **ok** | Metatron Overwatch Completed | executive_console | metatron_watch_tick · `mwt_6138d2ca549f48bc99cd` |
| `evt_7755171e5db24ef1b80d` | event | `receipt.created` | info | Receipt Created | system | receipt · `rcpt_e6586b451ce54a46beec` |

`ok` on `metatron.overwatch.completed` matches the classifier (`_OK_MARKERS` includes `completed`). It is not an invented score.

Contract match vs swarm `2210a144…`: item fields `id, source, kind, status, title, actor, entity_type, entity_id, stage, created_at, data` all present. `source ∈ {event, receipt}` in this page; other sources exist in the 44144 union (not sampled in limit=5).

---

## 5. BOSS-341 live evidence

Fail rule used (same as `loopback_preview_open_urls` / this cert): **FAIL if any `url` or `open_url` contains `127.0.0.1` or `localhost`.** `internal_url` loopback is **OK**.

Default `GET /v1/previews?project_id=boss-dogfood` → **200**, keys `previews`, `project_id`, **50** rows.

| Metric | Count |
|---|---|
| Rows inspected | **50** |
| Operator `url`/`open_url` with `127.0.0.1` or `localhost` | **0** |
| Leaked ids | **[]** |
| Absolute public-base gateway (`https://boss-api.local.sean.network/v1/previews/{id}/web/`) | **45** |
| Operator `url` and `open_url` both empty | **5** |
| `internal_url` is loopback | **45** (allowed) |
| Status | expired 39 · stopped 7 · failed 3 · verified 1 · **running 0** |

Empty operator urls (not leaks — no bind / not running):

| id | status | mode | internal_url |
|---|---|---|---|
| `prev_8a4947e073c547aab6d9` | expired | fixture | absent |
| `prev_2f44168b25be44b7bcb6` | failed | process | absent |
| `prev_9e32f33ccfa1435cbc98` | failed | process | absent |
| `prev_07d02e8345cc4ada8045` | failed | process | absent |
| `prev_fe4078ebe864487792d6` | verified | test | absent |

`publicize_preview` is supposed to leave failed/never-bound rows `url=null`. The expired fixture with no bind matches that. The verified-test row with no URL is fail-closed in UI (`operatorPreviewOpenHref` does not invent a gateway for verified-without-url).

---

## 6. What this is not

- Not a browser View HTML proof. List had **0 running**. Historical B2 proof (`prev_4289c3eaad02479daa47`) is now **expired** in this list (operator url is still the public-base gateway, not loopback).
- Not a 14-day `qa-live` day. Not Community GA.
- Not a close of BOSS-337.
- Plane states were **not** patched by this cert. Recommendation only.
- Activity API DB tests and web vitest were not run.

---

## 7. Residuals (do not block these two P0s)

- FEATURES.md “Global activity log” stays **◐ / Not GA**.
- No running preview in the current 50 — new-build View HTML is still a Phase B residual, **not** a 341 loopback fail.
- `apps/api/tests/test_activity_api.py` still wants a Postgres client fixture for HTTP shape/redaction/no-spend.
