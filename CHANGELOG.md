# Changelog

All notable changes to BOSS Community. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Release notes with more detail live in `docs/community/RELEASE_NOTES_*.md`.

## [Unreleased]

### Changed

- A build holds its heavy-build slot only while it works the machine (writing sessions, installs, tests, the app). While it designs and plans, the slot goes to the next build, and it takes it back in order. Waits for the slot count as queue time on every repair clock, and the build says what it waits for.
- On installs that never saved Setup › Agent capacity, job slots are now measured on the machine: heavy builds at their memory request, thinking builds at about 400 MB, up to 4. Before, they matched "Builds at once". Setup says why each number was chosen.
- The always-on system doctor adds a heavy slot when work has waited 10 minutes and the machine has had room for 10 minutes. It goes at most one above the measured value, drains when room goes (nothing is stopped), and backs off after a drain. Each episode is one row in "Decided for you"; Undo there removes the extra slot and keeps the doctor quiet for 24 h. A pinned "Builds at once" is never raised: the doctor says when your limit is what holds the queue. To turn the boost off, pin "Builds at once".

## [v1.0-rc10.32]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.32.md](docs/community/RELEASE_NOTES_v1.0-rc10.32.md).

### Added

- Enterprise networks: company CA at build and run time, registry mirrors, outbound proxy, an egress allowlist that adds to the built-in hosts, a lockfile for the coding CLIs, Colima guidance, and an opt-in engine inner sandbox (off by default). Much of it came from an enterprise contributor.
- The Work panel: one landing page per piece of work, and a one-line Work row.

### Changed

- Agents fix failures with the starter's authority, on by default (merge, production deploy, approvals, secrets and host commands stay with people). Turn it off in Govern or per project with `agents_fix_failures`.
- A remediation must ship an edit of its input; the validator's findings and fixes are recorded.
- Failing checks never read "Ready to merge"; health probes use declared checks; API previews open on routes that answer.
- Helm chat turns survive a reload or restart and land in their thread; `forget` retires knowledge-graph lessons.
- The community candidate version is `1.0.0-rc.10.32`.

## [v1.0-rc10.31]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.31.md](docs/community/RELEASE_NOTES_v1.0-rc10.31.md).

### Changed

- Project pages have an About this app card (spec summary, must-haves met or partly met, Read the full spec) and What was discussed / What was built for each piece of work.
- The Work row's button names the real next step; every change has Open, Start or Recreate preview; short titles come from the spec; the Outputs header matches Checks.
- Build pages open from any project; "Can't reach BOSS" shows only when BOSS is down; the sidebar keeps your current, newest and last-opened project.
- Chat windows have an Attach icon and a full-screen button; Helm replies are short with Details folded.
- Docs updates never ask for GitHub on local projects. The community candidate version is `1.0.0-rc.10.31`.

## [v1.0-rc10.30]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.30.md](docs/community/RELEASE_NOTES_v1.0-rc10.30.md).

### Changed

- Easy Mode runs a quick spec panel, then build, P1 tests, preview and deploy to staging (skipped in plain words when there is no server). It never auto-merges; you press Approve and merge. Every Easy Mode build gets its delivery workflow under the one consent at the start.
- Merge takes the approved head; a follow-up after merge opens its own change (local repos).
- Failed deploys say what BOSS is doing about them, and app crashes are caught in about 30 seconds.
- Every project page shows "Your app" with Open app; previews stay running while memory allows, can be recreated, and sleeping links wake.
- Every failure in BOSS has an agent owner: fixing, or one action item with the reason and Done - retry. The community candidate version is `1.0.0-rc.10.30`.

## [v1.0-rc10.29]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.29.md](docs/community/RELEASE_NOTES_v1.0-rc10.29.md).

### Changed

- The template gallery offers only what this install can build: Android and Godot templates removed, Orbit dodge is a browser game, every template names its toolchain, and a template whose toolchain is off shows "Needs Go" / "Needs Java" with the command; a launch of one is refused in words.
- Java and Go are built in by default (about 0.75 GB more). Existing installs get them on the next update unless `.env` sets `BOSS_TOOLCHAINS=none` or its own list. A toolchain change also rebuilds egress.
- Remote installs mark the host dedicated (`BOSS_DEDICATED_HOST=1`) when unset, so old build cache is cleaned automatically.
- Template cards show a drawing of each app and fold to two rows with Show all. The community candidate version is `1.0.0-rc.10.29`.
- The Build with BOSS composer has icons, tinted suggestion chips and a readable Easy Mode button.
- Top-bar menus (Builds, bell, account) keep their own width; Find a build was squeezed to its button.
- Light lane for normal builds (quality bar `standard` by default): review findings become follow-ups on a ready card; production stays strict.
- BOSS retries its own failures (max 2, spaced); person-side failures say what to do; failed builds are visible with fix/Retry; plain words throughout.
- Easy Mode asks "Confirm it's you" instead of failing; started card with Watch the build.
- Previews: no garbled Next.js pages, data kept across restarts, working App link.
- Security: BOSS's git and file work on a build's tree runs as the build's user.

## [v1.0-rc10.28]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.28.md](docs/community/RELEASE_NOTES_v1.0-rc10.28.md).

### Fixed

- First-run install, claim and sign-in: the claim step leads, the claim link works in an open tab, no seat question on a new install, local model off by default, `boss doctor` wording, side-by-side installs, `boss stop` / `boss start`.
- Command Center templates load; Easy Mode asks for consent, gives the project a local repository and opens its pull request.
- Try a sample shows every need up front and keeps its code on this machine; dead controls, counts and the project strip fixed.
- Host tools see org-shared servers; Confluence moved to Setup › Documents.

### Changed

- A freshly claimed install retires the shared console password (no second factor). The community candidate version is `1.0.0-rc.10.28`.

## [v1.0-rc10.27]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.27.md](docs/community/RELEASE_NOTES_v1.0-rc10.27.md).

### Added

- The preflight line says the detail. The learned row says the detail. The journey says the detail. The gateway test says the detail. The alert says what it is about. The custom step says its summary. The plugin says its description. The starter says its description. The backlog row says the detail. The extension says its description.

### Changed

- The community candidate version is `1.0.0-rc.10.27`. The alert line and the backlog label cell are divs so a long sentence can be a paragraph.

### Release scope

- This is the git tag of public main after rc10.26. It is not a live host deploy. No image tag or release-gate run is attached. The export record still cites `docs/community/RC9_VALIDATION.md`. A failed deploy phase still has no phase-only retry. A migration approval was not added. The journey timeline stays title-only. The alert rule button was not given a second sentence.

## [v1.0-rc10.26]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.26.md](docs/community/RELEASE_NOTES_v1.0-rc10.26.md).

### Added

- The last test says its summary. The workforce says why the control plane is unreachable. The send sheet says why that channel. The experiment says why that verdict. The pack says why there is no dashboard. The old deploy line says why it failed. The govern switch says why install is refused. A refused fix says the detail. The problem note says the message. The paused rule says why.

### Changed

- The community candidate version is `1.0.0-rc.10.26`.

### Release scope

- This is the git tag of public main after rc10.25, including pull requests 155, 156, and 158 which were already on main. It is not a live host deploy. No image tag or release-gate run is attached. The export record still cites `docs/community/RC9_VALIDATION.md`. A failed deploy phase still has no phase-only retry. A migration approval was not added. A second change-set blocked sentence was not added, because that reason is already on the page.

## [v1.0-rc10.25]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.25.md](docs/community/RELEASE_NOTES_v1.0-rc10.25.md).

### Added

- The deploy tag says why a deploy is not live. The deploy chip says why. Profile checks say what failed. A failure note says what to do. The database line says why Postgres was chosen. A fix card says the rejected guess. A linked ticket says its sync error. A paused webhook says its last error. The promote sheet says why evidence is short. A rejected option says why.

### Changed

- The community candidate version is `1.0.0-rc.10.25`.

### Release scope

- This is the git tag of public main after rc10.24. It is not a live host deploy. No image tag or release-gate run is attached. The export record still cites `docs/community/RC9_VALIDATION.md`. A failed deploy phase still has no phase-only retry. A migration approval was not added.

## [v1.0-rc10.24]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.24.md](docs/community/RELEASE_NOTES_v1.0-rc10.24.md).

### Added

- The stop pill says why agents are stopped. The merge button says why it is held. A plugin row says why it is blocked. An extension row says why it is blocked. A fix card says its confidence reasons. A recent finished run says its error. A ticket says why it was flagged. A call row says its error. An ask action says why. A takeover row says why.

### Changed

- The community candidate version is `1.0.0-rc.10.24`.

### Release scope

- This is the git tag of public main after rc10.23, including pull request 154 which was already on main. It is not a live host deploy. No image tag or release-gate run is attached. The export record still cites `docs/community/RC9_VALIDATION.md`. A failed deploy phase still has no phase-only retry. A migration approval was not added.

## [v1.0-rc10.23]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.23.md](docs/community/RELEASE_NOTES_v1.0-rc10.23.md).

### Added

- The project header says the health reason. A deploy card says the health reason. The cap preview names the stopped days. The limits screen names the paused days. An agent profile says its description. The identity overview names each access role. A pipeline route shows its description. A preview says its error. The waive sheet says why a check cannot be waived. A start proposal says why.

### Changed

- The community candidate version is `1.0.0-rc.10.23`.

### Release scope

- This is the git tag of public main after rc10.22. It is not a live host deploy. No image tag or release-gate run is attached. The export record still cites `docs/community/RC9_VALIDATION.md`. A failed deploy phase still has no phase-only retry. A migration approval was not added.

## [v1.0-rc10.22]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.22.md](docs/community/RELEASE_NOTES_v1.0-rc10.22.md).

### Added

- A plan card shows the full title when it says more than the short label. The staging tile says the health reason. A review step says its error. A stopped mission says its error. An org agent says its configured model. A savings row says the action. A preview says why restart is off. A workforce card says the autonomy level. A workflow definition shows its description. A deploy card says when a host is shared.

### Changed

- The community candidate version is `1.0.0-rc.10.22`.

### Release scope

- This is the git tag of public main after rc10.21. It is not a live host deploy. No image tag or release-gate run is attached. The export record still cites `docs/community/RC9_VALIDATION.md`. A failed deploy phase still has no phase-only retry. A migration approval was not added.

## [v1.0-rc10.21]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.21.md](docs/community/RELEASE_NOTES_v1.0-rc10.21.md).

### Added

- A schedule row says why it did not start. The dead-letter list says when more failures are off the page. The deploy card, the environment drawer, and the stage card say whether p95 is the app or probes. The project health chip shows the p95 number. The identity overview and the pulse say when more agents are off the page. A recent run says why it stopped. The safety trace says when more events are off the page.

### Changed

- The community candidate version is `1.0.0-rc.10.21`.

### Release scope

- This is the git tag of public main after rc10.20. It is not a live host deploy. No image tag or release-gate run is attached. The export record still cites `docs/community/RC9_VALIDATION.md`. A failed deploy phase still has no phase-only retry. A migration approval was not added.

## [v1.0-rc10.20]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.20.md](docs/community/RELEASE_NOTES_v1.0-rc10.20.md).

### Added

- A paused schedule is visible on the collapsed workflow card. Infra Find includes the approver. A deploy row names who approved it. Project deploy history shows the by name. The agent scorecard says when the last violation was. The workflow registry says when a list is longer than the page. Health and the staging tile say whether p95 is probes or the app. A notify policy chip shows the quiet-hours timezone. A command-center app card says how many builders are past the five faces.

### Changed

- The community candidate version is `1.0.0-rc.10.20`.

### Release scope

- This is the git tag of public main after rc10.19. It is not a live host deploy. No image tag or release-gate run is attached. The export record still cites `docs/community/RC9_VALIDATION.md`. A failed deploy phase still has no phase-only retry. A migration approval was not added. Origin already held pull requests 148 and 149; this tag includes that merge.

## [v1.0-rc10.19]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.19.md](docs/community/RELEASE_NOTES_v1.0-rc10.19.md).

### Added

- The reports page draws the selected window's days and agents, and shows subscription-included spend beside the settled figure. An activity export says when retention cut it and how many lines it wrote. The dashboard names the cursor, the ack backlog, and the canary counts. A workflow card shows the last run's status. Recent sends say when returned rows are off the page, and a failed send shows its error word. The activity rail says when it is showing 40 people. A finding path opens that lane's pull request. A tool-call export says when the cap hid older rows. The app Logs control says it opens the Activity log. A change-set lane shows its short commit and its diff counts.

### Changed

- The community candidate version is `1.0.0-rc.10.19`.

### Release scope

- This is the git tag of public main after rc10.18. It is not a live host deploy. No image tag or release-gate run is attached. The export record still cites `docs/community/RC9_VALIDATION.md`. A failed deploy phase still has no phase-only retry. A migration approval was not added.

## [v1.0-rc10.18]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.18.md](docs/community/RELEASE_NOTES_v1.0-rc10.18.md).

### Added

- Each health environment shows the stored latency objective, or says none is stored. The inbox shows a person only when the API already returned one. A succeeded staging run already on the deployments tab can be promoted with the existing action. A verify tail that names one failed test shows that name while the tail stays folded. A week deploy point shows the short commit it already stored. An activity export sets `X-Window-Capped` when retention cut the window. A tool-call export sets `X-Truncated` only when a further row exists past the cap. The agent scorecard says how many agents are past the first 12. The preview log drawer says the text is the process log.

### Changed

- The community candidate version is `1.0.0-rc.10.18`. Report windows now carry a daily chart and an agent list for that window. The page chart and the scorecard still read the flat series.

### Release scope

- This is the git tag of public main after rc10.17. It is not a live host deploy. No image tag or release-gate run is attached. The export record still cites `docs/community/RC9_VALIDATION.md`. A failed deploy phase still has no phase-only retry. A migration approval was not added.

## [v1.0-rc10.17]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.17.md](docs/community/RELEASE_NOTES_v1.0-rc10.17.md).

### Added

- The error page links its page digest to the Activity log and says that id is not a stored row. The first-week card links the Activity log and Govern. The Activity rail lists people. Recent sends can be found by subject or body. The deploy drawer names the recorded approver. A change-set lane opens its build. The build story marks lines newer than the last open. A latency objective can be stored in milliseconds.

### Changed

- The community candidate version is `1.0.0-rc.10.17`. The one-hour dashboard figure is labeled new reservations, not settled receipts. The number is unchanged.

### Release scope

- This is the git tag of public main after rc10.16, including the session spend ceiling already on main as pull request 145. It is not a live host deploy. No image tag or release-gate run is attached. The export record still cites `docs/community/RC9_VALIDATION.md`. A failed deploy phase still has no phase-only retry. A migration approval was not added.

## [v1.0-rc10.16]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.16.md](docs/community/RELEASE_NOTES_v1.0-rc10.16.md).

### Added

- Cycle time, deploy outcomes, and a shipped-versus-failed split of model spend. The noise report is on the AI SRE page. The notification outbox keeps the body and accepts a window and a text query. The activity log counts people. The reconciliation card imports an invoice and a seat price. Error-budget hours left, a count of silenced firing alerts, and a comparison with the previous window.

### Fixed

- `field_acl_records` in `db/schema.sql` closes, so a fresh schema applies. That close was already on main as pull request 146.

### Changed

- The community candidate version is `1.0.0-rc.10.16`. A custom activity window follows retention when that setting is longer than 30 days. The default stays 30 days.

### Release scope

- This is the git tag of public main after rc10.15. It is not a live host deploy. No image tag or release-gate run is attached. The export record still cites `docs/community/RC9_VALIDATION.md`. A migration approval was not added.

## [v1.0-rc10.15]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.15.md](docs/community/RELEASE_NOTES_v1.0-rc10.15.md).

### Added

- Easy Mode asks or blocks personal data before a budget reserve. A closed month refuses spend. One project invite binds that project only. A saved failure can be replayed into a new run. App log search, open work by person, and a stored availability objective.

### Fixed

- Report tables use the page window, and a finished failure is `failed_final` or `deadlettered`. A capped audit export says it is not the whole range. 99.5 is labeled the install default only until a person sets an objective.

### Changed

- The community candidate version is `1.0.0-rc.10.15`.

### Release scope

- This is the git tag of public main after rc10.14. It is not a live host deploy. No image tag or release-gate run is attached. The export record still cites `docs/community/RC9_VALIDATION.md`. A migration approval was not added.

## [v1.0-rc10.14]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc10.14.md](docs/community/RELEASE_NOTES_v1.0-rc10.14.md).

### Added

- Easy Mode launches a project from one description. The template catalog is included. A template does not build until launch.
- Agent, bot, and workflow wizards. Gap controls for preview, drafts, spend, prompts, security, and built-app access. One primary agent per person, bulk model change, trace export, and a voice transcript filed on a task.
- A brief records the required stack. A swapped stack asks. Add project stops when the build image lacks it.

### Fixed

- A merged pull request is shown as merged and is not opened a second time. People are named on wiki, comments, holds, and watches.
- A host must own the VM or deployment it acts on, and a host run fills git scope from its grant.

### Changed

- The community candidate version is `1.0.0-rc.10.14`.

### Release scope

- This is the git tag of public main after rc10.13. It is not a live host deploy. No image tag or release-gate run is attached. The export record still cites `docs/community/RC9_VALIDATION.md`.

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
