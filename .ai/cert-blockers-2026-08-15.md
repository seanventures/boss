# Named-blocker sweep — 2026-08-15

**Live tag:** `3af9c64` (api + worker + web)  
**Not public GA.** No `--env-template`. PR #174 not merged.

## Closed

### BOSS-369
- `BOSS_KMS_MASTER_KEY_FILE=/var/lib/boss/artifacts/.kms-master`
- `GET /v1/kms/health` `local_vault.ok=true` writable
- `sref_6b00a2d6a9424abba8a0` `storage_backend=local_vault`
- `prov_b8144adf9de042218b30` pointer `local_vault:BOSS_BOSS_DOGFOOD_OPENAI_COMPATIBLE_DOGFOOD_MODEL_BYOK_API_KEY`

### BOSS-351
- Flags: `BOSS_FACTORY_ALLOW_CREATE=1`, `BOSS_QA_ALLOW_CANARIES=1`
- run `crun_9ad921eab34f4d90bdfe` completed
- receipt `rcpt_cca90ba007e847af9ea2` `validate_sealed_receipt` ok
- decision `dec_8f58bea3cb644dc6bf27` pending hold
- https://github.com/seanheiney/boss/pull/174 open

## In flight

### BOSS-381
Official `force=true` `route=direct` runs. Honesty blob present. Do not invent scores.

| App | run_id |
|---|---|
| BugBox | `crun_01c41b278b8d4875a5f8` |
| HookWatch | `crun_d15a1f5556e946ab865a` |
| ApproveIt | `crun_95dacaa77a71484ab185` |

## Human / time HOLD

300 legal · 292 two partners · 293 / 314 / 337 umbrellas · 14-day qa-live still Day 1/14.
