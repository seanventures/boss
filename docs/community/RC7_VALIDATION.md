# RC7 validation

This record describes the checks behind the RC7 candidate. The release page identifies the published commit. Source publication, live deployment and permission to invite testers are separate milestones.

## Checks completed

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

The worker currently stops its heartbeat before sending the completion acknowledgment. Crash/reclaim fencing passed, but a delayed completion acknowledgment remains a qualification limit. Protect an uncertain attempt from duplicate effects and reconcile its outcome before retrying.

The release download contains source code and a checksum, rather than prebuilt container images. Check the pinned release instructions in the [release notes](RELEASE_NOTES_v1.0-rc7.md).
