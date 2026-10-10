# RC8 validation

This record describes the checks behind the RC8 candidate. The release page identifies the published commit. Source publication, live deployment and permission to invite testers are separate milestones.

## Integration status

RC8 includes the complete published RC6.2 history. Fresh installation, backend boundary checks and production builds are being repeated against the integrated candidate.

| Integrated check | Result and scope |
| --- | --- |
| Web console | On Node 20.20.1, all 6,004 tests passed with one skipped at `149ac22b`. This includes the RC6.2 UI changes and preserves the design refresh. Subsequent reserved-origin fixture changes at `bde33d49` passed 45 affected web tests; selections overlap the full suite. |
| Production web build | Node 20.20.1 production build and route generation/type check passed. |
| RC6.2 backend changes | 111 targeted engine/API tests passed against disposable PostgreSQL, including CI gates, app metrics, GitHub URLs, build-database setup, first-deploy planning and Captain backoff. These use owned HTTP/provider fixtures, not live provider accounts. |
| Installer source contract | 24 tests passed with one optional Docker test skipped; this includes actual pinned-checkout shell execution. Fresh-user installed-stack testing is recorded separately. |
| Documentation | Strict MkDocs build passed, and all 99 Bash/sh examples in the README, installer guide and community manual parsed with Bash. Parsing does not qualify their provider or host effects. |
| Build-service containment | At `bde33d49`, 57 affected backend checks passed with no skips against owned PostgreSQL. Real database/role deletion and read-only ledger failures remained visibly pending until writable retry succeeded. Independent review repeated 13 overlapping cleanup/reader cases. Docker context proof excluded application tests and scratch probes; final production-image proof is a separate step. |
| Architecture authority and approval | At `c55f68c1`, 95 selected engine checks and three API checks passed with no skips. Independent review passed 66 overlapping checks. These use actual PostgreSQL authority and scripted provider replies: reservation, leases and receipt commit before contact; replay does not resubmit; an unused expired lease releases only with its original ownership tokens; uncertain accepted spend stays held. Exact current-project PRD approval, cost evidence and scoped SDK retries are covered. Live provider/CLI behavior is not certified by these fixtures. |

The table below records the earlier preparation checks; it is historical evidence and does not certify the integrated release.

## Preparation checks completed

| Area | Evidence and scope |
| --- | --- |
| Web console | The complete suite on Node 20.20.1 passed 5,959 tests, with one skipped. A subsequent Home contrast correction passed 133 affected tests; its independent review passed 96 overlapping tests. Repeat selections are not added to the full-suite count. |
| Approval policy | 29 focused checks passed in independent review. Four real-Postgres checks proved manual review retains a waiting step, audit act and pending decision, while explicitly enabled automatic approval and ordinary gates retain their expected behavior. |
| Budget admission | 51 selected tests passed against real Postgres, including concurrent admission, expiry, period changes, conflicting settlement evidence and restart. Independent review repeated the same selection. |
| Backup and restore | Disposable Docker recovery exercised a real Owner, authenticator sign-in, encrypted connection recovery and artifact restoration. Invalid archives and missing or incorrect keys were rejected before target restoration. This does not qualify replay of later external effects from an older snapshot. |
| Repository isolation | Source checks and an actual Linux workload UID helper verified confined file access, resource limits, protected-marker denial and removal of control-plane credentials from workload Git. |
| Production API and worker | Disposable images exercised Owner authentication, cross-project denial, worker authority, actor attribution, a verified audit chain, harmless installed-worker execution, cancellation, heartbeat, crash/reclaim fencing and clean restart. These checks made no live provider calls. |
| Browser | Actual Owner sign-in, record-backed Home guidance, keyboard-operated task composition and a 390-pixel layout were checked against the running API. Draft retention was checked within the mounted composer; retention after reload or account changes is not claimed. |
| Documentation | The manual builds with strict MkDocs validation. Shell examples were syntax checked; this is not evidence that every documented provider or host workflow has been executed. |

Targeted source changes received independent review. The required engine lint subset passes. Broader inherited application and engine lint findings remain; this is not a blanket clean-lint claim.

## Before invitations

The following qualification remains open:

- A clean installation on each supported host and the complete upgrade lifecycle.
- A genuine useful result from a configured live provider, with its budget, receipt and artifact evidence.
- Reconciliation of legacy uncertain attempts and external effects omitted by an older restored snapshot.
- Uncoached first-run testing by people outside the build team.
- Verification that the hosted website serves this candidate's installer and guides.

An earlier first-use navigation from Home to Approvals stayed on its loading state until reload. In the fresh integrated installation, Approvals was still loading after 11.6 seconds and was present at the next 55.2-second observation without reload. With live updates paused, setup was still loading after 4.2 seconds and present at the next 18.6-second observation. These are observation bounds, not exact completion times. Authenticated full-document server renders took 0.301 seconds for Approvals and 0.063 seconds for setup in that fixture. Client navigation remains a qualification issue; a complete first-run journey has not passed.

Build-database cleanup retries retain process-local state. If the API dies after physical deletion but before its ledger update succeeds, the open durable ledger row can require reconciliation; automatic restart recovery of that retry is not claimed.

The worker currently stops its heartbeat before sending the completion acknowledgment. Crash/reclaim fencing passed, but a delayed completion acknowledgment remains a qualification limit. Protect an uncertain attempt from duplicate effects and reconcile its outcome before retrying.

The release download contains source code and a checksum, rather than prebuilt container images. Check the pinned release instructions in the [release notes](RELEASE_NOTES_v1.0-rc8.md).
