# Preview View — Community GA live proof checklist

**Status date:** 2026-08-13  
**Plane:** BOSS-341 (code), Phase B2 live View  
**Live image:** `boss-api:647c742` (api/worker/web shared tag; boot-fixed rollback from crashed `64d0e99`)  
**Code:** `engine/boss_engine/services/preview_service.py` (`publicize_preview`, gateway proxy)

---

## Invariant (always)

Operator-facing `url` / `open_url` must **never** be loopback (`127.0.0.1`, `localhost`, `::1`, `0.0.0.0`).

- DB / internal bind stays under `internal_url` for the API reverse proxy.
- Public surface is the gateway path: `/v1/previews/{id}/web/`  
  or absolute `{BOSS_PREVIEW_PUBLIC_BASE}/v1/previews/{id}/web/`.

When `BOSS_PREVIEW_PUBLIC_BASE` is set:

- Non-loopback origin → absolute operator URLs on **list** (`GET /v1/previews`) and start/smoke payloads.
- Loopback origin → **rejected**; fall back to relative gateway path (fail closed).
- Empty Settings field still reads process env `BOSS_PREVIEW_PUBLIC_BASE`.

---

## Live truth (2026-08-13, goldeneye dogfood)

Probed over SSH into `boss-app-api-1` (no secrets printed).

| Field | Live value |
|---|---|
| `BOSS_PREVIEW_PUBLIC_BASE` | `https://boss-api.local.sean.network` (set in API process **and** bound on `settings.preview_public_base`) |
| `GET /v1/previews?project_id=boss-dogfood&limit=50` | **200**, 50 rows |
| Status mix (2026-08-13, `647c742`) | expired 31 (30 process + 1 fixture) · failed 9 · stopped 6 · verified 3 · **running 1** |
| Operator `url` / `open_url` | Bound rows are `https://boss-api.local.sean.network/v1/previews/{id}/web/` |
| Relative `open_url` | **0** on bound running/historical process rows |
| Loopback as `url` / `open_url` | **0** |
| `internal_url` | loopback bind (expected; proxy-only) |
| Empty `open_url` | failed / never-bound / expired fixture (no dead Open link) |
| `GET /v1/previews/{id}/web/` (expired sample `prev_af60b33719b04082b027`) | **409** `preview not running (status=expired)` |

### Operator URL shape (current, proven)

```
https://boss-api.local.sean.network/v1/previews/{preview_id}/web/
```

Example from live list:

```
url:       https://boss-api.local.sean.network/v1/previews/prev_6bd9728fb32748b5864f/web/
open_url:  https://boss-api.local.sean.network/v1/previews/prev_6bd9728fb32748b5864f/web/
internal_url: http://127.0.0.1:41635
```

**List contract: PASS.**  
**View HTML (real app): PASS live (replay on `647c742`)** — `prev_4289c3eaad02479daa47` **running** (`mode=process`, `run_id=crun_7cc663627f2340dc85ac`, `build_substrate=local_subprocess`), GET **200** title `BugBox — capture, track, resolve`. Historical factory run (`npm run verify` exit 0, PR #83). `qa-working-software` **PASS**. Prior replay `prev_af60b33719b04082b027` expired. New greenfield canary `crun_27b5a74441fc4c0d8ace` still running — do **not** stamp Community GA.  
**View HTML (fixture):** historically **PASS** (`prev_8a4947e073c547aab6d9`); that fixture is now **expired** (409). Fixture path remains honest, not the GA bar.

---

## Live View proof (operator)

Run against the dogfood API after deploy of a SHA that includes publicize:

### 1. List contract

```bash
curl -sS -H "Authorization: Bearer $BOSS_API_KEY" \
  "${BOSS_API}/v1/previews?project_id=boss-dogfood&limit=5" \
  | jq '.previews[]? // .[]? | {id, status, url, open_url, internal_url}'
```

**Pass (live 2026-08-13):**

- For running (or historically bound) previews:  
  `url` and `open_url` are  
  `https://boss-api.local.sean.network/v1/previews/{id}/web/`  
  when `BOSS_PREVIEW_PUBLIC_BASE` is set.
- **Never** `http://127.0.0.1:…` or `http://localhost:…` as primary `url` / `open_url`.
- Loopback may appear only under `internal_url`.

### 2. Browser / curl View

Pick a **running** preview id (`prev_4289c3eaad02479daa47` on `647c742` as of this probe):

```bash
PREV=prev_…
# Relative base (API host):
curl -sS -o /tmp/prev.html -w "%{http_code}" \
  "${BOSS_API}/v1/previews/${PREV}/web/"
# Or absolute if BOSS_PREVIEW_PUBLIC_BASE is the public API origin.
```

**Pass (still required for B2 close):**

- HTTP 200 (or app redirect within gateway).
- Body is app HTML (not “no longer running” / empty error for a live preview).
- From a tailnet browser: open the same View URL; page loads without needing a tunnel to 127.0.0.1 on the API host.

**Current freeze:** expired/stopped/failed View returns **409** `preview not running (status=…)`. Replay View **does** return 200 HTML on `prev_4289c3eaad02479daa47`. Still do not claim “every preview always works” or stamp Community GA until a **new** build on this tag also produces a reachable preview.

### 3. Console

Mission Control Previews → **View** / Open uses `open_url` (gateway), not `internal_url`.

### 4. Config reference

```bash
# Example dogfood (set live):
BOSS_PREVIEW_PUBLIC_BASE=https://boss-api.local.sean.network
# Leave empty for relative paths when the console and API share origin.
```

---

## Automated coverage

| Test | Asserts |
|---|---|
| `engine/tests/test_preview_urls_are_operator_reachable.py` | Loopback rewritten; public base absolute; loopback base rejected |
| `test_list_payload_never_returns_loopback_open_url_when_public_base_set` | List JSON never emits 127.0.0.1 as `open_url` when public base is set |
| `test_list_previews_applies_public_base_and_never_emits_loopback_open_url` | `list_previews` wiring + settings |
| `test_publicize_falls_back_to_env_when_settings_field_empty` | Empty Settings field still reads `BOSS_PREVIEW_PUBLIC_BASE` |

---

## GA claim language

| Allowed now (replay View 200 on `647c742`) | Forbidden until a **new** build on this tag also Views 200 |
|---|---|
| “Preview list `open_url` is an API gateway URL operators can copy” | “Every preview is always reachable” |
| “Loopback binds are internal-only” | “Sandboxes/previews always work” / Community GA Done |
| “`BOSS_PREVIEW_PUBLIC_BASE` makes `open_url` absolute” | Marketing screenshots with `127.0.0.1` as the View link |
| “A replay BugBox preview opens as designed (`prev_4289c3eaad02479daa47`)” | “New greenfield builds on this tag are proven” |

Related: `docs/product/SANDBOX_GA_STATUS.md`, `docs/operations/PREVIEW_EXECUTION_SUBSTRATES.md`, `docs/operations/COMMUNITY_GA_CLAIMS_FREEZE.md`.
