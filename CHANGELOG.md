# Changelog

All notable changes to BOSS Community. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Release notes with more detail live in `docs/community/RELEASE_NOTES_*.md`.

## [v1.0-rc10.13]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.13.md](docs/community/RELEASE_NOTES_v1.0-rc10.13.md).

### Added

- A held tool call stores the digest of the arguments that were shown. Resume refuses a different call. The decision card shows the redacted arguments. The tool receipt refuses when that digest does not match. A decision with no digest still runs.
- Tool results are screened for instruction-override phrases. A hit holds the next write.
- A reachable probe quarantines a tool whose schema or description hash changed and opens one decision.
- Each receipt-path tool call is appended to an audit row. One project's rows can be listed.
- An always-allow row can be granted and revoked. The live allow/ask/off read is unchanged.
- Local changes review like a git change, on this machine and on GitHub.

### Changed

- The always-on agents ship on. Off remains one Govern choice, or `BOSS_AMBIENT_COS=off`.
- The community candidate version is `1.0.0-rc.10.13`.

### Release scope

- This is the git tag of public main after rc10.12. It is not a live host deploy. No image tag or release-gate run is attached. The export record still cites `docs/community/RC9_VALIDATION.md`.

## [v1.0-rc8]

Friends-and-family preparation. Notes: [docs/community/RELEASE_NOTES_v1.0-rc8.md](docs/community/RELEASE_NOTES_v1.0-rc8.md).

### Changed

- Includes the published RC6.2 setup, deployment planning, automatic build database, CI, metrics and architecture-approval changes; retains the Command Center and sign-in design refresh.
- First-run guides now follow the actual Owner, model-connection and task paths, with direct troubleshooting and feedback links.
- Home leads with the next action from current records; the task composer, templates and configured roles remain available.
- An unavailable sign-in-options read offers Retry. Help panels, focus states and primary controls are easier to use on small screens.
- Installer readiness has a bounded deadline and attempt count.

### Fixed

- Backups retain matching identity keys and validate archive contents before protected purge or fresh-volume restore.
- Workload-controlled Git runs below the workload user boundary. Repository readers and writers use confined, bounded file access.
- Concurrent project-budget admission shares one arbitration boundary; unsettled positive holds survive expiry and period changes. Settlement uses one spend snapshot and idempotent cost evidence.
- Discovery cancellation fences late work, receipts and retries against the owning run and job lease.
- Direct-change spec review honors manual-approval policy and retains its gate when settings cannot be read.
- Home primary buttons retain readable white text across normal and hover gradients.
- First area navigation preserves already rendered areas and their mounted unsent drafts.
- Model-connection and Standard Product Delivery guides match the actual proof records and single preview step.
- Ubuntu installation runs the local node agent with Bash, the shell its script requires.
- Automatic database detection uses the confined repository reader. Failed database or ledger cleanup stays visibly pending for retry instead of reporting success.
- Architecture versions commit a budget reservation, job/model leases and a receipt before model submission, and bind approval to the current project's exact PRD.
- Runtime build contexts exclude application test directories and scratch probes; identity fixtures use reserved example origins.

### Release scope

- Incremental release preserving the published RC6.2 ancestry and RC8 preparation history. Full private-export synchronization is still pending.
- This candidate does not certify every host, provider or restored-snapshot replay path. Read the release notes before enabling live work.

## v1.0-rc6.2

First-run setup without help (GitHub links and verification, deploy needs only a machine, automatic build database, step-up everywhere, claim discoverability, README quick start), opt-in CI workflow changes with Wait for CI, app traffic metrics, Architecture approval, and the Captain backoff. See docs/community/RELEASE_NOTES_v1.0-rc6.2.md.

## [v1.0-rc6.1]

Hotfix on v1.0-rc6. Notes: [docs/community/RELEASE_NOTES_v1.0-rc6.1.md](docs/community/RELEASE_NOTES_v1.0-rc6.1.md).

### Fixed

- **The console is fast again.** A page asks only for the feeds it shows (Factory 38 → 17 API calls, Overview 34 → 12), each signed-in request spends 2 identity statements instead of 4, and the Factory and Command Center send smaller payloads (283 → 134 KB, 1.16 MB → 642 KB). The console reuses an answer for up to 5 s only after the same page load proved the session live at the same identity epoch; revocation still takes effect on the next request.
- **The Infra Deploy button** returned a server error on every app target. Registering and bootstrapping a deployment target from setup had the same fault.

### Changed

- The engine's Python package version is `1.0.0rc6.post1` (PEP 440 has no `1.0.0-rc.6.1`); everything else is `1.0.0-rc.6.1`.

## [v1.0-rc6]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc6.md](docs/community/RELEASE_NOTES_v1.0-rc6.md).

### Added

- **Named accounts (Identity).** Every person signs in as themselves: passkeys, password with an authenticator app, recovery codes, email links and codes (with SMTP), OIDC and SAML single sign-on, social sign-in, SCIM provisioning and self sign-up by domain. Access roles (Owner, Admin, Operator, Viewer, Auditor and custom roles), groups, project-level access, service accounts, personal API keys and effective-access explanations. See [People and access](docs/community/PEOPLE_AND_ACCESS.md) and [Sign-in setup](docs/community/SIGN_IN_SETUP.md).
- **Claiming an install.** The first visit to `/claim` creates the first Owner. `boss identity setup-link` makes a one-hour claim link.
- **Agents act for a person.** Agent Passports with a ceiling and a sponsor, delegation grants with consent, run tokens, and a revocation cascade that halts runs at once. See [Agents and access](docs/community/AGENTS_AND_ACCESS.md).
- **boss-egress.** Builds hold no GitHub or provider credential; egress adds a per-build GitHub App token (or the person's own) and enforces the rules.
- **Build sandbox.** Each build runs as its own user with a fenced network. See [Build sandbox](docs/community/SANDBOX.md).
- **Security audit log.** Append-only, hash-chained, verifiable and exportable; 400-day retention with automatic, checkpointed pruning.
- **`boss identity`** host commands: `setup-link`, `claim-status`, `recover-owner`, `host-key`, `legacy`, `posture`, `audit`, `migrate`.
- **Host tools' key** (`BOSS_HOST_API_KEY`, service account "Host tools") used by `boss`, `boss doctor`, `make metatron` and `remote.sh`.
- **API key hygiene.** Expiry reminders 14 and 3 days ahead, optional revocation of keys unused for N days.
- `boss upgrade --no-rollback`.

### Changed

- The shared console password claims the install, then signs in as Operator for 30 days after the claim (one Owner extension of up to 30 days), then stops. Every page shows the countdown.
- The shared `BOSS_API_KEY` loses people-and-access changes at the claim and stops at the same date. Responses on any legacy credential carry `Deprecation` and `Sunset` headers.
- `make mcp-setup` creates a project service account and a key pinned to that project, instead of a member bound in `BOSS_API_KEYS`.
- Access checks are enforced by default (`BOSS_AUTHZ_MODE=enforce`).
- `docker compose exec api` runs as root; pass `-u boss`.
- `boss upgrade` puts the previous release back when the new one cannot start.
- rc5 SSO providers are copied into Identity › Sign-in & SSO, switched off.
- A boss key works in `X-Boss-Api-Key` as well as `Authorization: Bearer`.
- `GET /v1/auth/session` also returns the signed-in principal, org, MFA time, CSRF token, expiry and a key's pinned projects.

### Fixed

- A wrong password no longer fails the sign-in with a server error.

## [v1.0-rc5]

Model router, governed host deployment, the redesigned console and Improve BOSS. See [docs/community/RELEASE_NOTES_v1.0-rc5.md](docs/community/RELEASE_NOTES_v1.0-rc5.md).

## Earlier

`v1.0-rc1` to `v1.0-rc4`: see the GitHub releases page.
