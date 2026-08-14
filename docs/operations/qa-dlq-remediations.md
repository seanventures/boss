# QA — DLQ remediations (BOSS-342 residual / A5)

**Date:** 2026-08-13  
**Branch:** `ai/grok/community-ga`  
**Plane:** BOSS-372 (residual of Done BOSS-342 worker git image)  
**No live spend.** Operator path only.

## Why this exists

Live dogfood still shows **41 historical open deadletters**. Classify
(`POST /v1/deadletters/classify`) already buckets them. Remediations were
not first-class: incident + requeue existed, but there was no list of
operator actions and retry could not leave a durable row when blocked.

Every operator action now writes `deadletter_remediations` + events.
Retry/requeue/hold/escalate never silently drop.

## Operator API

| Method | Path | Effect |
|---|---|---|
| `GET` | `/v1/deadletters` | Existing feed (41 historical rows stay visible) |
| `POST` | `/v1/deadletters/classify` | Existing `git` / `sandbox` / `model` / `github` / `other` |
| `GET` | `/v1/deadletters/remediations` | List remediations (`deadletter_id` optional) |
| `POST` | `/v1/deadletters/{id}/remediations` | Attach `retry` \| `requeue` \| `escalate` \| `hold` |
| `POST` | `/v1/deadletters/{id}/retry` | Retry + always persist applied/blocked/failed |
| `POST` | `/v1/deadletters/{id}/requeue` | Existing job requeue (unchanged) |
| `POST` | `/v1/deadletters/{id}/incident` | Existing incident packet (used by `escalate`) |

Unknown `action` → **400** (no row). Missing DLQ → **404**. Non-job retry
→ **200** with `status=blocked` and a visible remediations row +
`deadletter.remediation_blocked` event. Already-requeued retry is
idempotent (`already_requeued=true`, same job id).

## Classifier (already fixed)

| Text | Class | Capacity escalate? |
|---|---|---|
| `OpenRouter credits exhausted` | `model` | yes |
| `Groq TPM` / `429` / `rate limit` | `model` | yes |
| `bad credentials` / GitHub auth | `github` | no — **not** model |
| `Missing credential for SecretRef …DIGITALOCEAN…` | `other` | no |
| `microvm_claims_frozen` | `sandbox` | no (already exists) |
| `sandbox_task_missing` | `sandbox` | no (already exists) |
| `No such file or directory: 'git'` | `git` | no |

`credits?\\b` must not swallow `credentials`. Tests lock that split.

## Remaining live classes (historical 41)

Snapshot from `GA_LIVING_PLAN.md` / live dogfood 2026-08-13. These are
**not** closed by this operator path; remediations make them actionable.

| Live class | Count (hist) | After this change | What still has to happen |
|---|---|---|---|
| **git / missing-git** | 1 historical | **0 new after worker image** (`git` in `boss-worker` image; BOSS-342 Done) | Do not requeue the old missing-git row unless a shared-tag worker has already replaced it. New code.task must not mint `missing_git`. |
| **model TPM / credits / 429 / max-turns** (claude-143 cluster) | 21 | Still open | Raise TPM/credits or wait for quota; classify escalates to a Decision. Retry only after capacity is real. **No live spend from this slice.** |
| **missing DO creds** (`SecretRef env:BOSS_DIGITALOCEAN_TOKEN`) | 18 | Still open (`other`) | Paste/bind a DigitalOcean token via Community easy-connect, then retry the job deadletter. Not a classifier bug. |
| sandbox host 404 / `sandbox_task_missing` | 1 | Class already `sandbox` | Host task is gone — retry cannot recreate it. Hold or escalate. |
| `microvm_claims_frozen` | 0 in this 41 | Class already `sandbox` | Expected under `BOSS_MICROVM_CLAIMS_FROZEN`. Do not requeue to “make preview work”. |

## Operator playbook

1. `POST /v1/deadletters/classify` with `escalate_model=true`.
2. `GET /v1/deadletters/remediations?project_id=…` — confirm no silent
   history (empty list is valid).
3. Per item:
   - **git (post-image):** only retry if worker tag includes git.
   - **model TPM:** wait / raise limits; do not hammer retry.
   - **missing DO creds:** bind SecretRef, then `POST …/retry`.
   - **sandbox_task_missing / claims_frozen:** `hold` or `escalate`.
4. Confirm retry: remediations `status=applied`, deadletter
   `status=requeued`, new job `queued`. Blocked retries stay on the
   remediations list.

## Tests (no live spend)

- `engine/tests/test_deadletter_classify.py` — credit vs credential +
  sandbox reason codes.
- `apps/api/tests/test_deadletter_classify.py` — classify + Decision
  escalation (existing).
- `apps/api/tests/test_deadletter_remediations.py` — list/create, retry
  visible state, blocked non-job, 404, unknown action.

## Out of scope

- preview_service, meetings, comms, packs.
- Live DigitalOcean / model spend.
- Auto-draining the 41 historical rows.
