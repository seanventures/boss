# Remaining human / time blockers (not agent-closable)

**Date:** 2026-08-15  
**Live tag:** `3af9c64`  
**Not GA.** Agents must not close these.

Agent-closable named blockers from this sweep are **done**:

| Ticket | Result |
|---|---|
| BOSS-369 | Done — durable `BOSS_KMS_MASTER_KEY_FILE`, `local_vault.ok=true`, SecretRef `sref_6b00a2d6a9424abba8a0`, provider `prov_b8144adf9de042218b30` |
| BOSS-351 | Done — sealed canary `crun_9ad921eab34f4d90bdfe`, receipt `rcpt_cca90ba007e847af9ea2`, hold decision `dec_8f58bea3cb644dc6bf27`, [PR #174](https://github.com/seanheiney/boss/pull/174) **not merged** |

## BOSS-381 — official QA reports (in flight, not human)

Not a human legal gate. Forced official runs are live; close only when `/report`
exists and honesty holds. Do not invent scores. ui/ux may stay `not_scored`.

- BugBox `crun_01c41b278b8d4875a5f8`
- HookWatch `crun_d15a1f5556e946ab865a`
- ApproveIt `crun_95dacaa77a71484ab185`

## BOSS-300 — OSS licensing (human legal)

Canonical gate: `docs/operations/OSS_LICENSING_DECISION.md` (still **UNDECIDED**).

Owner: legal counsel + product owner. Options remain A/B/C in
`docs/planning/oss-readiness/DECISION-oss-licensing-strategy.md`.

**Required human action:** pick one option, set `Status: DECIDED` with date and
counsel reference. Do not ask an agent to choose. Do not flip the repo public
until this is DECIDED **and** BOSS-293 C1–C8 pass.

## BOSS-292 — Design-partner self-serve

Code/docs path exists (`make onboard`, private week-1 notes). The ticket DoD
also requires **two** partners through self-serve with P0s clear for two weeks.

**Required human action:** invite two design partners and run the week-1 path.

## BOSS-293 / 314 / 337 — Public flip + GA umbrellas

Still blocked by:

- BOSS-300 legal
- GitHub private-repo protection / public flip hygiene
- **14 consecutive qa-live days** (`docs/operations/QA_LIVE_14DAY.md` is still
  Day 1/14 — do not backfill)
- Official Cloud/Enterprise FEATURES stay Partial by design

The sealed weekly proof now exists (PR #174, hold). That is **one** factory PR
success, not 14 qa-live days and not a public-GA stamp.

**Required human action:** decide legal + whether to treat 2026-08-16 as Day 2
of the qa-live clock.

Do not stamp public GA from this file.
