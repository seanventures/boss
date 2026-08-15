# BOSS-381 — official QA /reports (scorecard v1.1)

**Probed:** 2026-08-15T16:02Z–16:07Z  
**Branch:** `ai/grok/boss-381-official-reports` from `main` `bd4cc36`  
**Live:** `https://boss-api.local.sean.network` · project `boss-dogfood` · edition `community`  
**Shared tag:** `3af9c64` (`boss-api` + `boss-worker` + `boss-web`)  
**Flags (live `.env`, counts only):** `BOSS_FACTORY_ALLOW_CREATE=1` `BOSS_QA_ALLOW_CANARIES=1` `BOSS_QA_ALLOW_OFFICIAL=1`  
**Auth:** `X-Boss-Api-Key` from live `/srv/apps/boss-app/.env` (never printed)  
**Plane:** `79f85c21-40e7-45ff-8751-c9cedf41c2a1` — **leave In Progress**  
**Not public GA.** PR #174 not merged. No `--env-template`.

## Close gate (this cert)

| Check | Result |
|---|---|
| Three `/reports` exist | **yes** |
| `honesty.honest=true` | **yes** (all three) |
| `invented_scores=[]` | **yes** (all three) |
| Close Plane? | **No.** ApproveIt is still running (`implement`). Live tag still has the PR-open bug (fix is on this branch, not deployed). |

Named residual: **ApproveIt `crun_95dacaa77a71484ab185` not terminal** + **live `3af9c64` still dumps full diffs into the review-packet LLM call**.

## Official runs (live)

| App | run_id | status / phase | `/report` | honest | invented_scores | passed | ui | ux | decision |
|---|---|---|---|---|---|---|---|---|---|
| BugBox | `crun_01c41b278b8d4875a5f8` | blocked / done | 200 | true | `[]` | false | scored 100 (scan) | **not_scored** | `dec_3071f58fe7c1448e8665` pending hold |
| HookWatch | `crun_d15a1f5556e946ab865a` | blocked / done | 200 | true | `[]` | false | scored 75 (scan) | **not_scored** | `dec_ba29a3e6e6c94fa19797` pending hold |
| ApproveIt | `crun_95dacaa77a71484ab185` | **running / implement** (16:02Z) | 200 | true | `[]` | false | **not_scored** | **not_scored** | none |

Job for ApproveIt: `job_2f10906d84be4d0cbe1b` `running` lease until 16:24Z. Phase moved write_tests → implement. **Not stuck.** Do not kill.

Reservation IDs are in-memory Community `oss_res_*` (`NoopBudgetPort`). Sealed canary `crun_9ad921eab34f4d90bdfe` used the same port and **did** open [PR #174](https://github.com/seanheiney/boss/pull/174) (not merged).

## Why official PRs died after 3 attempts

Not factory flags (they are on). Not github plane (events show `mission_control_binding` `paramg/boss-sandbox` `ghrepo_cc1b3856114c493d8b63`).

Attempt 1 (receipts):

- `rcpt_038ecbe99167499dbdb9` BugBox `github.pull_request.create` **failed**
- `rcpt_cb5f5eb417a24f178097` HookWatch `github.pull_request.create` **failed**

Error (verbatim class): LiteLLM/Groq `context_length_exceeded` on `model_group=gpt-4o` while `propose_pull_request` dumped **every file body** into the review-packet prompt (BugBox 63 checkpointed files, HookWatch 60).

The handler then **released the reservation** and marked the receipt failed. Attempts 2–3 therefore returned `blocked` / `Missing active reservation for PR proposal` (those hold decisions). Error string on the runs: `PR not created after 3 attempts: blocked`.

Sealed canary succeeded because its diff was small enough for the same prompt.

## Code fix on this branch (not live)

`engine/boss_engine/services/software_factory.py`:

- `review_packet_file_summaries` / `review_packet_prompt` send path + bytes + 240-char preview (max 40 files)
- `_review_packet_markdown` falls back to a static packet on model error
- Review-packet failure **does not** fail the receipt or release the reservation
- Fallback text does not invent UI/UX scores

Live tag `3af9c64` does **not** have this. Do not redeploy with `--env-template`. Do not merge #174.

## Honesty (do not invent)

Workspace scan attached real `ui_evidence` (`a11y_markers`, `states_covered`). That is why BugBox/HookWatch **ui is scored**. UX JTBD rows have `passed=null` → **ux stays `not_scored`**. No `a11y_violations=0`. No JTBD pass invented.

Catalog `GET /v1/qa/benchmark-apps?project_id=boss-dogfood` is still stale: HookWatch `built=crun_911888eefbd14166bce6` (404, not a QA report). Inspect now accepts `BOSS_OFFICIAL_RUN_IDS=bugbox=…,hookwatch=…,approveit=…`.

## Tests run

```
PYTHONPATH=.:engine .venv/bin/python -m pytest -q \
  engine/tests/test_report_honesty.py \
  engine/tests/test_propose_pr_review_packet.py \
  engine/tests/test_propose_pr_review_continues.py \
  scripts/tests/test_official_reports.py \
  engine/tests/test_github_dual_plane.py
```

**20 passed.**

## Residual (381 stays open)

1. ApproveIt official run `crun_95dacaa77a71484ab185` still `running`/`implement` — wait for a terminal `/report` (honest, even if `passed=false`).
2. Live `3af9c64` still cannot open official PRs until the review-packet fix is on a new shared tag (no `--env-template`).
3. Catalog `built` still does not point at these official run IDs.

Do not stamp public GA. Official Cloud/Enterprise FEATURES stay Partial.
