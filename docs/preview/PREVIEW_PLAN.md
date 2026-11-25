# Guided preview plan

A guided preview for a preview team of 10–20 developers who will use BOSS to automate their API
gateway onboarding pipeline. Not production. Ticket: BOSSAPP-1283 (epic BOSSAPP-1282).

| Doc | For |
|---|---|
| [ONBOARDING.md](ONBOARDING.md) | A developer's first day |
| [FREEZE.md](FREEZE.md) | The feature freeze until the journeys are green |
| [PILOT_CHECKLIST.md](PILOT_CHECKLIST.md) | The five pilot blockers, verified on the install |
| [SECURITY_REVIEW_PACK.md](../security/SECURITY_REVIEW_PACK.md) | The security and compliance review |
| [OKTA_GROUPS_TO_ROLES.md](../operations/OKTA_GROUPS_TO_ROLES.md) | Access for the team through Okta |
| [API_GATEWAY_ONBOARDING.md](../design/API_GATEWAY_ONBOARDING.md) | The pipeline the team automates |
| [SUPPORTED_STACKS.md](../community/SUPPORTED_STACKS.md#preview-matrix) | Which stacks work, cell by cell |

## Scope

**In scope**

- API gateway config onboarding: validate a partner's file, reject or fix it, staging, contract
  tests, rollback, promotion with approval.
- Ticket to pull request: a Jira ticket becomes a plan, a build, an acceptance check and a PR.
- Repositories on GitHub cloud and GitHub Enterprise Server.
- Stacks: Java/Spring (Maven, Gradle), Go, Node (React, Next.js, Angular), Python, as the
  [matrix](../community/SUPPORTED_STACKS.md#preview-matrix) says today.
- Sign-in through Okta (SSO and SCIM), roles from Okta groups.
- Your own model keys; budgets; human approval for production; audit export.

**Out of scope**

- Production traffic and production data.
- Mobile (SwiftUI, React Native) and ML stacks (TensorFlow, LangChain apps).
- Kubernetes and Helm deploys (not built; Docker on an SSH host only).
- Air-gapped installs.
- MicroVM isolation (frozen; builds run in the fenced sandbox).

## Golden journeys

Each runs end to end on a real install with a real model. A journey is green only when BOSS's
Acceptance Agent accepts the outcome (`engine/boss_engine/services/acceptance/verdict.py`).

| # | Journey | Depends on |
|---|---|---|
| J1 | Jira ticket → plan → build → acceptance → PR (GitHub cloud, Node/React) | — |
| J2 | The same on a local or Enterprise Server repo, in Go | GHES proof (BOSSAPP-1286) |
| J3 | A Java/Spring service with Maven | — |
| J4 | One change across two or more repos | BOSSAPP-1363 |
| J5 | A failure, a retry, a recovery | — |
| J6 | Approval gate → deploy to staging → promote | — |
| J7 | Okta sign-in with group-to-role mapping | Okta tenant |
| J8 | API gateway onboarding end to end (bad config rejected, fix PR, staging, contract tests, approval, production) | the full chain ([design](../design/API_GATEWAY_ONBOARDING.md#what-is-on-main-today)) |
| J9 | Post-deploy error → alert → runbook remediation | BOSSAPP-1293 |

The daily runner for these journeys (BOSSAPP-1284) and the green-day counter (BOSSAPP-1285) are
described in [FREEZE.md](FREEZE.md#what-exists-today). The console journeys in
`tests/e2e/harness/journeys.py` are UI checks, not these.

## Readiness bar

The preview opens when all of these hold:

1. Every golden journey green **5 days in a row** on both hosts ([FREEZE.md](FREEZE.md)).
2. Every item in the [pilot checklist](PILOT_CHECKLIST.md) shown on the preview install.
3. The [security pack](../security/SECURITY_REVIEW_PACK.md) reviewed and approved by the
   preview team's security reviewers.
4. An independent agent follows [ONBOARDING.md](ONBOARDING.md) on a fresh install with no help.
5. The release gates pass ([TEST_TIERS.md](../operations/TEST_TIERS.md)).

## Success metrics

Agreed for the pilot review (BOSSAPP-1292):

| Metric | Measured in BOSS today? |
|---|---|
| Time from PR open to approval, before and after | **Not yet** |
| Deploys blocked because a regression failed after merge | **Not yet** |
| Post-deploy errors alerted, and how many were auto-remediated | **Not yet** |
| Cost per run and per team against the monthly budget | Per run yes (receipts); per team **not yet** (BOSSAPP-1295) |

## How a developer starts

Follow [ONBOARDING.md](ONBOARDING.md). Report anything that blocks you through the feedback path
in [FEEDBACK_LOOP.md](../community/FEEDBACK_LOOP.md).
