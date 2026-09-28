# Full BOSS Community public release checklist

**System of record:** [BOSS-293](https://plane.local.sean.network/goldeneye/projects/cc97d5cc-e8c6-4893-ac14-7f646c37f3d7/issues/35ec645f-b01a-40ed-9930-639aa513b770)
**Target:** `seanventures/boss`, a reviewed full Community export
**Current target state (2026-09-28):** private and empty
**Verdict:** not ready for public visibility or a Community GA announcement

This supersedes the older engine-only extraction path for the requested full
Community repository. The private BOSS monorepo remains the integration source.
Only the reviewed export candidate and its approved history may be pushed to
the public target. Cloud, Enterprise, secrets, and internal-only material stay
outside the export. [OSS_LICENSING_DECISION.md](OSS_LICENSING_DECISION.md)
records the product owner's Apache License 2.0 decision and counsel reference
`01049940`; the remaining gates still need evidence.

## Required gates

| Gate | Evidence required | Current state |
|---|---|---|
| PF-1 License implementation, BOSS-300 | Decision record, approved root `LICENSE` in the exact export, package metadata and links consistent, third-party terms retained, reviewed implementation PR merged | **Open.** Counsel reference `01049940` and PR #278 are merged; the candidate includes `LICENSE`. The [preliminary dependency inventory](COMMUNITY_DEPENDENCY_LICENSE_REVIEW.md) flags third-party terms for final-image and counsel review. |
| PF-2 Community/paid boundary | Export manifest and checker pass on the exact candidate; no Cloud/Enterprise code or internal-only files; API, worker, web, TUI, MCP and installer runtime acceptance | **Open.** A 1,783-file interim candidate had zero mechanical blockers, but public-reference hygiene and final runtime acceptance are still in progress. |
| PF-3 Reachable-history secret review | Full gitleaks and second scanner or approved equivalent on the history to be published; each finding dispositioned; live credentials rotated; clean final scan | **Open.** The private source history currently has 13 redacted gitleaks findings, including a committed environment backup. The intended target will start with a new root commit; its exact reachable history still needs two clean scans and finding dispositions. |
| PF-4 Security and dependency review | Private reporting channel, dependency/license audit, must-fix security review, and authenticated console proxy with live TLS/CSRF proof | **Open.** Console session protection is integrated and local TLS-proxy checks passed; deployment credentials, live TLS/CSRF proof, API write authorization, and dependency review remain. |
| PF-5 Fresh VM installation | Piped one-line install on a clean supported x86 VM, health/auth/edition, loopback binding, rerun preservation, upgrade, backup, restore, uninstall/reinstall; repeat on final export | **Open.** Source-tree x86 Ubuntu 24.04 lifecycle passed in a disposable guest. The exact final Community export still needs its own clean VM acceptance. |
| PF-6 Live Community QA | Fourteen consecutive daily `qa-live` passes and the required working-software proof on the release commit; no backfill | **Open.** Goldeneye artifacts show 47 consecutive daily PASS reports through 2026-09-28, including the last 14 days. See [the calendar](QA_LIVE_14DAY.md). The proposed release commit lacks a shared-tag pass, current working-software proof, and Ops/QA signoff. |
| PF-7 Claims and partner readiness | README, docs, release notes and site comply with [claims freeze](COMMUNITY_GA_CLAIMS_FREEZE.md); partner notice or explicit zero-partner owner sign-off | **Open.** Review after candidate and partner inventory are final. |
| PF-8 Release controls | Reviewed merge SHA, reproducible release tests, exact export commit and checksum, rollback target, branch protection and secret scanning configured on target, release owner approval | **Open.** Target is empty/private; no release candidate or target protection evidence. |
| PF-9 Public publication | PF-1 through PF-8 complete; owner records visibility change, public URL, tag, release notes, and first-hour monitoring on BOSS-293 | **Not started.** |

BOSS-293 remains In Progress until every required row has linked evidence.
`make public-flip-check` is a small mechanical subset. A passing result never
substitutes for scans, runtime qualification, a 14-day streak, or owner approval.

## Publication procedure after the gates pass

1. Freeze the exact reviewed Community export commit and checksum. Re-run its
   tests, dependency/license audit and reachable-history secret scans. Attach
   reports and finding dispositions to BOSS-293.
2. Verify the target repository's branch protection, secret scanning, push
   restrictions, and private security reporting. GitHub Actions stay disabled
   unless a separate approved change enables them.
3. Send partner notice before visibility changes, or record the product owner's
   explicit zero-partner sign-off. Review all release copy against the claims
   freeze and live evidence.
4. Push the approved export and protected release tag to `seanventures/boss`.
   Change visibility only after verifying the pushed commit and history match
   the reviewed candidate. Record actor, time, repository URL and tag in Plane.
5. Monitor secret-scanning alerts, install reports, support and first-hour
   issues. If a problem appears, halt promotion, rotate exposed credentials,
   and publish a correction. A public-to-private visibility reversal does not
   retract clones, forks or caches, so do not treat it as secret rollback.

## Mechanical check

```bash
make public-flip-check
```

The checker reads the historical planning decision status, private reporting
presence, claims document, checklist presence and tracked credential-looking
paths. It does not inspect full git history, export coverage, runtime behavior,
license notices, partner status, or live QA. Its exit status must be interpreted
with the gate table above.

## Evidence history

| Date | Event |
|---|---|
| 2026-08-13 | Engine-only checklist created; public flip blocked. |
| 2026-09-24 | Product owner selected a full Community repository at `seanventures/boss` and Apache License 2.0; export and history review started. |
| 2026-09-28 | Product owner confirmed counsel approval (reference `01049940`). Checklist revised to the full Community boundary. Public release remains gated. |
| 2026-09-28 | PR #278 merged the license decision; clean source-tree x86 installer lifecycle and 47-day live QA artifact streak were observed. Final export, history, security, and release-commit proof remain open. |
