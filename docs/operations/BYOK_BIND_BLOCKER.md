# BYOK bind blocker

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
