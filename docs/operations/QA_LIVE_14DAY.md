# Community GA — 14-day `qa-live` calendar

- **Observed through:** 2026-09-28 06:15 UTC (read-only artifact review)
- **Host artifacts:** Goldeneye `/var/lib/boss/qa-live/`
- **Schedule:** `15 6 * * * /usr/local/bin/boss-qa-live-daily.sh`
- **Gate:** `scripts/qa/live_dogfood_gate.py` (10 required checks + optional Metatron)

## Observed streak and release status

The host has a JSON report and nonempty log for **every UTC day from 2026-08-13
through 2026-09-28: 47 consecutive days**. All 47 reports say `passed=true`,
required **10/10**, and optional Metatron **1/1**. No day was backfilled in this
review. The previous Day 1/14 calendar was stale.

**PF-6 remains open for the proposed Community release.** The dated reports do
not record the QA script's source commit, the app's deployed commit, or an
independent execution ID. The report filename is the available artifact ID.
The current live API, worker, and web containers were created on 2026-09-28
at 05:02 UTC from images tagged `main-7b3ce2f3-202609280458`, before that
day's 06:15 UTC gate. This ties the latest pass to that older deployed tag;
it does not qualify a later Community export or release candidate. Record the
exact release source and deployed image digests/commits with a fresh shared-tag
`qa-live` and working-software run, then obtain Ops/QA signoff before closing
PF-6 or using Community GA language.

## Last 14 daily reports (redacted)

Each report below has a matching nonempty `.log` at the same path. `—` means
the report and log do not identify that commit. The report stem identifies the
artifact; the JSON has no separate `run_id` field. Times are UTC start times.

| Date | Start | Report stem | Required + optional | QA source SHA | Deployed app SHA at run |
|------|-------|-------------|---------------------|---------------|-------------------------|
| 2026-09-15 | 06:15:01 | `qa-live-20260915` | 10/10 + 1/1 PASS | — | — |
| 2026-09-16 | 06:15:01 | `qa-live-20260916` | 10/10 + 1/1 PASS | — | — |
| 2026-09-17 | 06:15:02 | `qa-live-20260917` | 10/10 + 1/1 PASS | — | — |
| 2026-09-18 | 06:15:01 | `qa-live-20260918` | 10/10 + 1/1 PASS | — | — |
| 2026-09-19 | 06:15:02 | `qa-live-20260919` | 10/10 + 1/1 PASS | — | — |
| 2026-09-20 | 06:15:02 | `qa-live-20260920` | 10/10 + 1/1 PASS | — | — |
| 2026-09-21 | 06:15:01 | `qa-live-20260921` | 10/10 + 1/1 PASS | — | — |
| 2026-09-22 | 06:15:01 | `qa-live-20260922` | 10/10 + 1/1 PASS | — | — |
| 2026-09-23 | 06:15:02 | `qa-live-20260923` | 10/10 + 1/1 PASS | — | — |
| 2026-09-24 | 06:15:01 | `qa-live-20260924` | 10/10 + 1/1 PASS | — | — |
| 2026-09-25 | 06:15:01 | `qa-live-20260925` | 10/10 + 1/1 PASS | — | — |
| 2026-09-26 | 06:15:01 | `qa-live-20260926` | 10/10 + 1/1 PASS | — | — |
| 2026-09-27 | 06:15:01 | `qa-live-20260927` | 10/10 + 1/1 PASS | — | — |
| 2026-09-28 | 06:15:01 | `qa-live-20260928` | 10/10 + 1/1 PASS | — | `7b3ce2f3` (live image tag) |

The latest working-software proof identified in the repository is
[`GA_LIVING_PLAN.md`](GA_LIVING_PLAN.md): **4/4 PASS on 2026-08-14** for the
BugBox, ApproveIt, and HookWatch sample-app path. No newer working-software
report was found in the host's inspected BOSS QA artifact directories. That
older proof does not establish working software on the proposed release
commit.

**Close condition:** At least 14 consecutive dated PASS reports with logs are
present, and the exact release commit, deployed shared image tag/digests,
current working-software proof, and Ops/QA signoff are linked in Plane. A
same-day rerun does not add a day; a missed day restarts the count.
