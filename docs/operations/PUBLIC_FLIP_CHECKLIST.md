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
| PF-2 Community/paid boundary | Export manifest and checker pass on the exact candidate; no Cloud/Enterprise code or internal-only files; API, worker, web, TUI, MCP and installer runtime acceptance | **Open for final source/release review.** Source `0db60f6c` exported 1,880 selected files with zero mechanical blockers. Its [pristine x86 VM report](/Users/sean/.codex/visualizations/2026/09/28/01a0e916-4f7e-7c91-b3e6-a8bbeeeca9bb/community-ga-0db60f6c/qualification-report.md) passed API/worker/web, representative API/TUI/MCP operations, and Grok/Claude/Codex presence in API/worker. Mapping all routes does not prove every route executable; later TUI/MCP changes require a new exact export and review. |
| PF-3 Reachable-history secret review | Full gitleaks and second scanner or approved equivalent on the history to be published; each finding dispositioned; live credentials rotated; clean final scan | **Open.** The `0db60f6c` unpublished one-root commit `af200616` exactly matches all 1,880 export entries. Full-history Gitleaks found zero; TruffleHog without verification found 11 unverified fixture/identifier detections and zero verified findings. [Sanitized report](/Users/sean/.codex/visualizations/2026/09/28/01a0e916-4f7e-7c91-b3e6-a8bbeeeca9bb/community-ga-0db60f6c/history-scan-summary.md). Four credential fields in removed private-source history still need provider-side revocation/rotation evidence. No target history has been pushed; rescan immediately before publication. |
| PF-4 Security and dependency review | Private reporting channel, dependency/license audit, must-fix security review, and authenticated console proxy with live TLS/CSRF proof | **Open.** Project-scoped API read/write guards, Plane effect receipts, and verified Plane TLS are integrated. Ungoverned Community deployment/host actions, Discovery research, investigations, raw SQL and setup model probes fail closed. Source `0db60f6c` classifies all 986 Community HTTP operations: 854 API-policy eligible, 29 exact human-confirmation gated, 36 held pending route-specific safeguards, 67 protocol/internal excluded. The [security review](/Users/sean/.codex/visualizations/2026/09/28/01a0e916-4f7e-7c91-b3e6-a8bbeeeca9bb/mcp-confirmation-security-review/review.md) is conditional; the installed VM passed 11 MCP confirmation checks and auth-disabled header denial. Live TLS/CSRF proof and exact-image dependency/notice disposition remain. |
| PF-5 Fresh VM installation | Piped one-line install on a clean supported x86 VM, health/auth/edition, loopback binding, rerun preservation, upgrade, backup, restore, uninstall/reinstall; repeat on final export | **Open for final source and public delivery.** Exact `0db60f6c` passed fresh Ubuntu 24.04 x86 first install, eight smoke gates, rerun preservation, backup/restore, same-tree and changed-code upgrade, safe uninstall/reinstall, and a second pristine stdin-piped install without a sudoers workaround. The [sanitized report](/Users/sean/.codex/visualizations/2026/09/28/01a0e916-4f7e-7c91-b3e6-a8bbeeeca9bb/community-ga-0db60f6c/qualification-report.md) distinguishes the tested local-file pipe from the untested public HTTPS URL. Later TUI/MCP source changes will need new VM qualification. |
| PF-6 Live Community QA | Fourteen consecutive daily `qa-live` passes and the required working-software proof on the release commit; no backfill | **Open.** Goldeneye artifacts show 47 consecutive daily PASS reports through 2026-09-28, including the last 14 days. See [the calendar](QA_LIVE_14DAY.md). The proposed release commit lacks a shared-tag pass, current working-software proof, and Ops/QA signoff. |
| PF-7 Claims and partner readiness | README, docs, release notes and site comply with [claims freeze](COMMUNITY_GA_CLAIMS_FREEZE.md); partner notice or explicit zero-partner owner sign-off | **Open.** The [release-notes draft](COMMUNITY_GA_RELEASE_NOTES_DRAFT.md) states verified features and paused paths. Claims/partner review and owner sign-off remain. |
| PF-8 Release controls | Reviewed merge SHA, reproducible release tests, exact export commit and checksum, rollback target, branch protection and secret scanning configured on target, release owner approval | **Open.** [Target audit](COMMUNITY_TARGET_CONTROLS_1A8102.md): rechecked 2026-09-28 as private with zero branches; GitHub Actions are disabled. Draft source PR #280 reports no hosted checks, so validation currently relies on local evidence. GitHub rejects branch protection/rulesets under the current private plan; secret scanning is disabled. No target rollback SHA exists. The release owner must resolve the control sequence or plan before publication. |
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
| 2026-09-28 | The 1,816-file export from `29daa8ca` passed its mechanical boundary check. A local new-root candidate history was scanned with Gitleaks and TruffleHog; the clean x86 VM run and public-release gates remain open. |
| 2026-09-28 | The interim x86 export completed its clean-VM lifecycle and runtime checks. Additional security review found host-action and model-spending gaps; the final source export and VM repeat remain open. |
| 2026-09-28 | Source `37f44df7` passed the 1,879-file mechanical export, unpublished one-root history scan, and pristine x86 VM lifecycle. That VM exposed a missing Grok CLI in API/worker; the source fix and later main merge require another exact export and VM. |
| 2026-09-28 | Exact source `0db60f6c` passed 1,880-file export and one-root scan; a clean x86 VM passed installer/runtime/lifecycle/confirmation checks and a second pristine guest passed stdin-piped installation with local file transport. Public HTTPS delivery, third-party terms, old private-source credential receipts, live release QA, and target controls remain open. |
