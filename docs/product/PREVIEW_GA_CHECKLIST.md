# Preview View — Community GA live proof checklist

**Status date:** 2026-08-12  
**Plane:** BOSS-341 (code), Phase B2 live View  
**Code:** `engine/boss_engine/services/preview_service.py` (`publicize_preview`, gateway proxy)

---

## Invariant (always)

Operator-facing `url` / `open_url` must **never** be loopback (`127.0.0.1`, `localhost`, `::1`, `0.0.0.0`).

- DB / internal bind stays under `internal_url` for the API reverse proxy.
- Public surface is the gateway path: `/v1/previews/{id}/web/`  
  or absolute `{BOSS_PREVIEW_PUBLIC_BASE}/v1/previews/{id}/web/`.

When `BOSS_PREVIEW_PUBLIC_BASE` is set:

- Non-loopback origin → absolute operator URLs.
- Loopback origin → **rejected**; fall back to relative gateway path (fail closed).

---

## Live View proof (operator)

Run against the dogfood API after deploy of a SHA that includes publicize:

### 1. List contract

```bash
curl -sS -H "Authorization: Bearer $BOSS_API_KEY" \
  "${BOSS_API}/v1/previews?project_id=boss-dogfood&limit=5" \
  | jq '.previews[]? // .[]? | {id, status, url, open_url, internal_url}'
```

**Pass:**

- For running (or historically bound) previews:  
  `url` and `open_url` are `/v1/previews/.../web/` or  
  `https://boss-api…/v1/previews/.../web/` when `BOSS_PREVIEW_PUBLIC_BASE` is set.
- **Never** `http://127.0.0.1:…` or `http://localhost:…` as primary `url`.
- Loopback may appear only under `internal_url`.

### 2. Browser / curl View

Pick a **running** preview id:

```bash
PREV=prev_…
# Relative base (API host):
curl -sS -o /tmp/prev.html -w "%{http_code}" \
  "${BOSS_API}/v1/previews/${PREV}/web/"
# Or absolute if BOSS_PREVIEW_PUBLIC_BASE is the public API origin.
```

**Pass:**

- HTTP 200 (or app redirect within gateway).
- Body is app HTML (not “no longer running” / empty error for a live preview).
- From a tailnet browser: open the same View URL; page loads without needing a tunnel to 127.0.0.1 on the API host.

### 3. Console

Mission Control Previews → **View** / Open uses `open_url` (gateway), not `internal_url`.

### 4. Config reference

```bash
# Example dogfood:
BOSS_PREVIEW_PUBLIC_BASE=https://boss-api.local.sean.network
# Leave empty for relative paths when the console and API share origin.
```

---

## Automated coverage

| Test | Asserts |
|---|---|
| `engine/tests/test_preview_urls_are_operator_reachable.py` | Loopback rewritten; public base absolute; loopback base rejected |

---

## GA claim language

| Allowed after live checklist green | Forbidden until then |
|---|---|
| “Preview View uses an API gateway URL operators can open” | “Every preview is always reachable” |
| “Loopback binds are internal-only” | Marketing screenshots with `127.0.0.1` as the View link |

Related: `docs/product/SANDBOX_GA_STATUS.md`, `docs/operations/PREVIEW_EXECUTION_SUBSTRATES.md`.
