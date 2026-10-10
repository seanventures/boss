# BOSS Community 1.0.0-rc.8

RC8 prepares BOSS for friends-and-family testing. It builds on public RC6.1 and preserves the existing contributor history.

## What changed

- **A clearer first run.** The guides follow installation, claiming an Owner account, connecting a model and choosing one useful task. Sign-in uses a passkey or a password with a second factor. A stored model credential is distinguished from a successful governed model call. Help includes direct troubleshooting and feedback links.
- **One next action on Home.** Current decisions, setup needs, stopped work, active runs and results lead with their actual destination. The existing task composer remains available; templates and configured roles open below current work. Missing evidence stays unknown.
- **Recoverable sign-in failures.** An unavailable sign-in-options read shows Retry instead of assuming that the install has no Owner. Primary controls have clearer contrast and focus states; help stays usable on small screens.
- **Bounded installer waiting.** Readiness checks have a deadline and attempt limit, so a service that cannot start produces a visible failure.
- **Safer backup and restore.** Complete archives include the matching identity key, database and artifacts. Archives and image/source compatibility are checked before protected purge or restore into fresh volumes. Restore starts only the database; application and external-effect workers remain stopped until the operator verifies recovery.
- **Stronger repository boundaries.** Workload-controlled Git runs under the workload identity without control-plane credentials. Repository access is confined to opened directories, rejects links and nonregular files, and bounds retained content.
- **Corrected financial admission.** Concurrent callers share project-budget arbitration. Outstanding positive holds remain counted across expiry and daily/monthly boundaries. Cost and held spend are read together, and duplicate settlement must agree with the recorded evidence. Temporary admission contention and an unresolved provider outcome have distinct visible states.
- **Manual approval stays manual.** Direct-change spec review honors the Owner's automatic-approval setting. A missing or unreadable policy keeps the review gate and its audit record.
- **Discovery Stop and retry safeguards.** Cancellation checks the owning run, job lease and effect phase. Late or uncertain effects remain visible for reconciliation; Stop does not reverse work already accepted by a provider.

## Install or upgrade

Use the [Quickstart](QUICKSTART.md) or the [installer guide](https://github.com/seanventures/boss/blob/v1.0-rc8/scripts/install/README.md). For a new checkout:

```bash
git clone --branch v1.0-rc8 https://github.com/seanventures/boss.git
cd boss
./install.sh --dir "$PWD" --ref v1.0-rc8
```

The website installer may be deployed separately from this source release. The pinned GitHub checkout above selects RC8 directly. An existing install should take a complete protected backup before upgrading. Changing the checkout back to an older release does not undo database migrations.

Backups contain encrypted connection data and the key needed to recover it. Protect the archive as a credential. Do not attach it, your `.env`, session cookies or model credentials to a bug report.

## Financial and recovery limits

Budget admission reserves an estimate; it is not a hard limit on a provider's eventual invoice. Actual overruns must still be recorded. An expired reservation cannot authorize another call. An accepted or uncertain attempt keeps its hold until its outcome is settled or a known no-call release is recorded. Review the reconciliation decision before retrying.

This correction does not reconstruct exposure that an older release already discarded. Before enabling live work on an upgraded install, reconcile outstanding legacy attempts and provider records. Likewise, restoring an older snapshot can omit later external effects: recovering the database and keys does not qualify replay. Keep effect workers stopped until external outcomes and applicable idempotency safeguards have been checked.

## Validation and remaining qualification

The release changes have targeted source tests and independent review. Recovery tests used disposable Docker databases and fresh volumes with a real Owner, authenticator sign-in and vault decryption. Repository isolation tests used the actual Linux UID helper against owned markers. Financial tests used real Postgres concurrency and restart schedules. These checks have different scopes; they are not proof of every live provider or host.

The production API and worker were also exercised in a disposable Docker installation: canonical Owner sign-in, tenant and worker authority, audit verification, a harmless job, cancellation, heartbeat, crash/reclaim fencing and clean restart passed. Browser checks covered actual sign-in, the Home next action and responsive task composition. See the [validation record](RC8_VALIDATION.md) for the final scope.

Supported-host clean installation, a genuine useful provider result, restored-snapshot effect reconciliation and uncoached human testing remain open. First-use area navigation also stalled in the browser fixture until a full reload; this remains an invitation blocker. The worker stops its heartbeat before sending its completion acknowledgment; delayed completion remains a qualification limit. RC8 publication alone does not qualify invitations or deploy the hosted website.

Several capabilities remain held behind their existing safeguards: live debugging investigations, direct host changes outside an approved plan, cloud-account deploy runs, auto-deploy, the agent host shell and host-backed preview sandboxes. Readable records do not imply these execution paths are enabled.

## Source publication

This is an incremental public release, rather than a wholesale private-source export. Reviewed public changes preserve RC6.1 ancestry and use preparation provenance. Historical private/public synchronization remains pending; the full-export guard has not been waived. See [Publishing](publishing.md).

The release download is source code, with a checksum for the attached source bundle. It is not a prebuilt container image. The installer program retains its independent version; the application reports `1.0.0-rc.8`, and the Python engine package reports `1.0.0rc8`.
