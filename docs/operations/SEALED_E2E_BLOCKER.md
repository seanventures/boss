# Sealed E2E blocker — BOSS-351 slice 9

**Date:** 2026-08-15  
**Plane:** BOSS-351 (`c1f3b9c6-923e-4b6e-b211-939934f908c5`) — **Done**  
**Live API:** `https://boss-api.local.sean.network`  
**Shared tag:** `3af9c64` (api + worker + web)  
**Not GA.** No `--env-template`. No merge.

## Resolved 2026-08-15

Operator set `BOSS_FACTORY_ALLOW_CREATE=1` and `BOSS_QA_ALLOW_CANARIES=1` on
`/srv/apps/boss-app/.env` (edit in place). One live canary:

| Field | Value |
|---|---|
| run | `crun_9ad921eab34f4d90bdfe` completed / phase `done` |
| reservation | `oss_res_936f7bf8ab3b4850b183` |
| receipt | `rcpt_cca90ba007e847af9ea2` `github.pull_request.create` succeeded |
| `validate_sealed_receipt` | ok |
| PR | https://github.com/seanheiney/boss/pull/174 **open, not merged** |
| decision | `dec_8f58bea3cb644dc6bf27` pending, `default_action=hold`, `review_pull_request`, `no_auto_merge=true` |
| quality gate | `qg_a4096229b46d410b8996` |

Inbox JSON omits `reason` (the string `merge_decision_required` is on the
`decision.created` event). Hold is still the default. Do not merge #174 from
this file.

---

The rest of this file is the original 2026-08-15 probe (flags absent on
`b6d8ccc`). Keep it as history.

Operator probe (defaults to dry-run): `scripts/qa/sealed_e2e_live.py`.

---

## Verdict

A live sealed factory path was **not** started. The goldeneye runtime
(`/srv/apps/boss-app/.env`) does not allow a governed `code.task` create.

**Missing flags (names only):**

- `BOSS_FACTORY_ALLOW_CREATE`

**Similar / related flags also absent (names only):**

- `BOSS_QA_ALLOW_CANARIES`
- `BOSS_FACTORY_OWNER`
- `BOSS_FACTORY_REPO`

GitHub **is** configured on the live stack. That is not the blocker.
`GET /health` on tag `b6d8ccc` returned
`{"status":"ok","database_connected":true,"github_configured":true,"openai_configured":true}`.
Factory WIP advertises a Mission Control binding and `admit: true`
(`available_slots: 5`). Those facts do not override the missing allow-create
flag.

---

## Why this is a blocker, not a silent skip

Slice 9 is: start **one** narrow governed `code.task` with `open_pr=True`
(title like `sealed-e2e-canary`) via `POST /v1/code-tasks` and `X-Boss-Api-Key`,
then prove a Receipt plus a hold Decision. Do not merge.

`BOSS_FACTORY_ALLOW_CREATE` is the hard operator confirm used by
`scripts/qa/factory_e2e_gate.py` and `scripts/qa/sealed_e2e_live.py`. It is
**absent** from live `.env`. Without it this session must not queue budgeted
GitHub work.

`BOSS_QA_ALLOW_CANARIES` is the sibling live-PR / canary switch used by the
factory and harness gates. It is also absent.

`BOSS_FACTORY_OWNER` / `BOSS_FACTORY_REPO` are the factory-gate target
aliases. They are absent; the live process instead has
`BOSS_GITHUB_OWNER` / `BOSS_GITHUB_REPO` and
`BOSS_TARGET_GITHUB_OWNER` / `BOSS_TARGET_GITHUB_REPO` set (plus
`BOSS_GITHUB_TOKEN`). The script accepts those aliases. They do not satisfy
allow-create.

---

## What was checked (no secrets)

| Check | Result |
|---|---|
| Shared tag on api/worker/web | `b6d8ccc` |
| `--env-template` | not used |
| `BOSS_FACTORY_ALLOW_CREATE` | **ABSENT** |
| `BOSS_QA_ALLOW_CANARIES` | **ABSENT** |
| `BOSS_FACTORY_OWNER` / `BOSS_FACTORY_REPO` | **ABSENT** |
| `BOSS_GITHUB_OWNER` / `BOSS_GITHUB_REPO` | SET |
| `BOSS_TARGET_GITHUB_OWNER` / `BOSS_TARGET_GITHUB_REPO` | SET |
| `BOSS_GITHUB_TOKEN` | SET (value not printed) |
| `BOSS_API_KEY` | SET (value not printed) |
| `GET /health` | 200, `github_configured=true` |
| `GET /v1/factory/wip` | 200, `admit=true`, `fixture_supported=true`, binding present |
| `GET /v1/code-tasks` | 200 (existing runs; none is this slice's canary) |
| `GET /v1/receipts` | 200; latest rows were process/metatron ticks, not `github.pull_request.create` for a sealed-e2e-canary |
| `GET /v1/decisions/inbox` | 200, 92 items; no new sealed-e2e-canary hold from this slice |
| `POST /v1/code-tasks` this slice | **not sent** |
| Merge / approve | **not attempted** (`BOSS_ENABLE_SANDBOX_MERGE` is truthy on live — ignored) |

WIP `live_pr_success` on this tag is labeled
`39/40 from existing receipts (not a sealed weekly proof)` with `proven: false`.
That is **not** a slice-9 seal.

---

## Unblock

On goldeneye `/srv/apps/boss-app/.env` (edit in place — **never**
`--env-template`):

1. Set `BOSS_FACTORY_ALLOW_CREATE=1`.
2. Optionally set `BOSS_QA_ALLOW_CANARIES=1` if other QA canary paths should
   also be allowed.
3. Recreate api+worker so the process env picks the flags up (same tag,
   `--no-deps --force-recreate`; no template).
4. Re-run:

```bash
BOSS_API_KEY=… BOSS_FACTORY_ALLOW_CREATE=1 \
  python3 scripts/qa/sealed_e2e_live.py --live
```

The script still defaults to dry-run. `--live` posts **one** canary and never
merges. Close BOSS-351 only when that run proves a Receipt (`effect_kind`
`github.pull_request.create`) **and** a hold-default Decision.

---

## Out of scope

- Community GA stamp
- Merging the canary PR
- Inventing a live-PR success rate
- Simulated `open_pr=true` fixtures (API 422; not a sealed path)
