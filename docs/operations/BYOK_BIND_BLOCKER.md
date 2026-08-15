# BYOK bind blocker

**Resolved 2026-08-15** on shared tag `3af9c64`. Durable
`BOSS_KMS_MASTER_KEY_FILE=/var/lib/boss/artifacts/.kms-master` (chmod 600, not
ephemeral). `GET /v1/kms/health` `local_vault.ok=true` writable.

Live bind (no raw secret in this file):

| Field | Value |
|---|---|
| SecretRef | `sref_6b00a2d6a9424abba8a0` `storage_backend=local_vault` active |
| Provider | `prov_b8144adf9de042218b30` `openai_compatible` active |
| Pointer | `local_vault:BOSS_BOSS_DOGFOOD_OPENAI_COMPATIBLE_DOGFOOD_MODEL_BYOK_API_KEY` |
| last4 | not a field on this SecretRef schema (honest absence) |

Plane BOSS-369 is Done. **Not GA.**

---

Original probe (vault master missing on `b6d8ccc`):

Live bind of an existing env model key into `local_vault` SecretRef did not
complete. This file lists **flag and schema names only**. It does not contain
secret material.

**Not GA.**

## Attempt

- Endpoint: `POST /v1/providers/easy-connect`
- Backend: `local_vault`
- Provider: `openai_compatible`
- HTTP status: `503`

## Flag / schema names

- `BOSS_KMS_MASTER_KEY`
- `BOSS_KMS_MASTER_KEY_FILE`
- `BOSS_KMS_ALLOW_EPHEMERAL_MASTER`
- `local_vault`

## Operator follow-up (names only)

- Production `local_vault` write-through needs a durable master key flag. Do not use `--env-template`. Do not enable the test-only ephemeral master flag on goldeneye.
- Do not paste keys into tickets or logs.
