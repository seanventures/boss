# BOSS-369 live BYOK write-through — BLOCKED

**Date:** 2026-08-15  
**Ticket:** BOSS-369 `a8cf92b4-77ab-4298-be98-1ed79a1180a2`  
**Live:** `https://boss-api.local.sean.network` (shared tag `b6d8ccc`, Community)  
**Project:** `boss-dogfood`  
**Verdict:** **not bound.** No `local_vault` SecretRef for openai/anthropic.

## Named blocker

**vault master missing**

`GET /v1/kms/health` and `POST /v1/providers/easy-connect` both fail closed:

```
local_vault requires BOSS_KMS_MASTER_KEY or BOSS_KMS_MASTER_KEY_FILE
(or BOSS_KMS_ALLOW_EPHEMERAL_MASTER=true for tests only)
```

Enabling a live master key / ephemeral master would be new host config. Ticket forbids that. No bind attempted with a real model key.

## What is already true (not a local_vault bind)

| Surface | Result |
|---|---|
| `GET /health` | 200 `openai_configured=true` |
| `GET /v1/provider-definitions` | 200; `openai` + `anthropic` `adapter_status=available` |
| `GET /v1/providers?project_id=boss-dogfood` | 200; 4 accounts, **zero** `local_vault:` pointers |
| `GET /v1/secret-refs?project_id=boss-dogfood` | 200; 7 refs, all `storage_backend=environment`, **last4=null**, no `value`/`secret_value` |

OpenAI account already exists as an **env pointer only**:

- provider_account `prov_78c91c3fca5d47a8846a`
- label `dogfood-openai-byok`
- status `degraded`
- `credential_refs.primary` = `env:OPENAI_API_KEY`
- secret_ref `secret_7c6bc09399b24e77a58b` — `ref_kind=env`, `ref_key=OPENAI_API_KEY`, **no last4**

No anthropic provider account. No anthropic secret-ref.

## Live env model key (presence only)

API container env names/lengths (values never printed or written):

| Name | Presence |
|---|---|
| `OPENAI_API_KEY` | PRESENT `len=25` (set; unusually short vs typical `sk-` material) |
| `ANTHROPIC_API_KEY` | MISSING |
| `GROQ_API_KEY` | PRESENT `len=56` |
| `OPENROUTER_API_KEY` | PRESENT `len=73` |
| `DEEPSEEK_API_KEY` | PRESENT `len=35` |
| `BOSS_KMS_MASTER_KEY` | MISSING |
| `BOSS_KMS_MASTER_KEY_FILE` | MISSING |
| `BOSS_KMS_ALLOW_EPHEMERAL_MASTER` | MISSING |
| `BOSS_KMS_VAULT_DIR` | MISSING |

A live OpenAI env var exists, so a paste payload *could* be assembled without printing. Write-through still cannot complete: vault master is absent.

## Easy-connect live behavior

Route is mounted (Community, not 404):

| Probe | HTTP | Notes |
|---|---|---|
| `POST /v1/providers/easy-connect` `{}` | 422 | `Invalid easy-connect payload` |
| missing `secret_value` | 422 | `secret_value is required` |
| short dummy (`xyzwv`) | 422 | `too short (minimum 8)`; dummy not echoed |
| 8+ char dummy, `backend=local_vault`, `read_only=true`, `test_after=false` | **503** | vault-master error; dummy not echoed |

After the 503 probe:

- probe label `dogfood-openai-byok-local-vault-probe` was **not** created
- provider count still 4
- secret-ref count still 7
- still **no** `local_vault` pointer

Code path matches tests: `apps/api/app/routers/provider_easy_connect.py` → `ResourcePlaneService.easy_connect` → `put_secret(..., backend=local_vault)` → `SecretUnavailable` → HTTP 503.

## Why this is not success

- No `secret_ref_id` from a local_vault write-through
- No `last4` for openai/anthropic
- Existing openai bind is `env:OPENAI_API_KEY` only
- Binding now requires operator-set `BOSS_KMS_MASTER_KEY` or `BOSS_KMS_MASTER_KEY_FILE` on the live api (and a redeploy **without** `--env-template`)

## Unblock (operator, not this agent)

1. Generate a 32-byte urlsafe-base64 master and set `BOSS_KMS_MASTER_KEY` (or a 0600 `BOSS_KMS_MASTER_KEY_FILE`) on `boss-app` api+worker.
2. Recreate api/worker on the same shared tag. Do not pass `--env-template`.
3. Re-probe `GET /v1/kms/health` until `local_vault.ok=true`.
4. One easy-connect write-through of the existing live `OPENAI_API_KEY` (`read_only=true`, `test_after=false`). Confirm `secret_ref_id` + `last4` only; GET secret-refs must not leak the raw value.

Do not stamp GA. Do not invent a bind.
