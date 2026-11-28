# BOSS Community 1.0.0-rc.10.38 candidate

RC10.38 carries all 43 fixes an enterprise contributor sent from validating rc10.35 in a locked-down, local-only setup (Codex only, corporate CA and mirror, AppArmor). It adds a fault kit so failure cases are one command, and BOSS now knows where it was installed so it can fix itself. Each fix was reviewed again here, and many were widened to cover the whole class of bug.

This is a test candidate. Cutting the tag does not deploy a host or publish an image.

## What is better for you

**Installs, upgrades and doctor**

- The installer names a missing or empty configuration entry (for example `BOSS_DB_PASSWORD=`) and stops before replacing anything.
- The registry check prints its "verified" line only when every registry really verified.
- `boss clone <name>` accepts a project's display name or slug, in any case, and refuses a name two projects share.
- A Git push prints the console's real address and port, never `localhost:3000`.
- `boss doctor` reports a local repository correctly.

**Disk and storage**

- An upgrade now brings the build cache within its cap and prints Docker's real before and after.
- Doctor and Govern › Storage count only cache Docker can free. Shared layers are shown as a note, never as a warning with a command.
- Old BOSS images have their own row, with `boss prune`.
- **BOSS never clears another project's build cache.** A full clean-up runs only on a Docker marked as BOSS's own (`BOSS_DEDICATED_HOST=1`) or on BOSS's own buildx builder. Elsewhere BOSS prints the reading and the exact command. Added deployment servers get the same rule, with a "Runs only what BOSS deploys" checkbox.
- When the host helper stops reporting, the Storage card says the reading is old and names `boss steward status`. A storage hold is never released without a fresh host reading.
- Development installs keep scheduled backups within their cap on their own; production still asks. The newest three always stay.
- Rollback images are labelled for this install and removed only when the upgrade receipt names them.
- Observability › System lists each service's state from the host, marked stale when the helper is silent.

**Builds, reviews and merges**

- A merge card is bound to the commit it shows. A newer commit raises a new card and withdraws the old one. Merge is refused with "stale head" if the card is older than the change.
- Approving a local merge while sandbox merging is off keeps the card pending and names the setting, instead of claiming it merged.
- A spec with a blocking product question (for example public or admin-only) holds the build until it is answered, and the answer reaches the build. A missing readiness line no longer stalls unattended runs.
- A pushed documentation-only change gets a read-only review instead of failing with "no test files". Anything that can execute keeps the full checks.
- With a single model, the test author gets one correction when it wrote no tests.
- A recovery decision gets one bounded retry when the model's answer was malformed.
- Budget quotes and reservations use one calculation, so a 1-cent cap refuses a 2-cent request.
- A worker no longer floods logs when the API is briefly down.

**Console**

- Work rows show why a build is waiting, and a GitHub wait never reads as failed.
- Every action that needs your consent opens the consent sheet. A test calls all 151 write actions to keep it that way.
- A Command Center template can be built Into an existing project.
- Infra's Open uses the preview gateway, never an internal `127.0.0.1` address.
- A person promoted to Owner before setting up a second factor is offered "Sign in again" to enrol, instead of an admin reset.

**BOSS knows where it is installed**

- Every install and upgrade records the install folder and commit, and keeps BOSS's own source mirror current, offline.
- Govern › Self-fix shows "Ready to fix itself" with one action for each missing piece.
- On a development install the self-fix helper is set up automatically; turning it off sticks.
- A self-fix can never change what the host runs (installer scripts, compose files, Dockerfiles). Those need a person.

**Testing faster**

- `boss qa fault <case> --project <name>` creates a test-plan fault (REC-01 to 14, Node, Python and Go) on a branch and pushes it. `--revert` puts it back, `--list` shows them all. It refuses production projects and never pushes to the default branch.
- `boss qa report` builds the results ledger from BOSS's own records: IDs, unattended Y/N, time to fix, who acted.
- `boss qa disk-pressure warn|critical|floor|off` reaches the low-disk cases without filling the disk. It only raises thresholds, refuses production, and puts them back on its own after 24 hours.

## Upgrade notes

- Run `boss upgrade` or `remote.sh update` as usual.
- Installs that never saved "Builds at once" now get capacity sized from the machine (up to 4 job slots), and the doctor may add one more on an idle machine. Pin "Builds at once" in Setup to keep a fixed number.
- **On a Docker that runs only BOSS** (for example a dedicated Colima profile), set `BOSS_DEDICATED_HOST=1` in `.env` so upgrades can bring the build cache within its cap on their own.
- Existing development installs with several people turn self-fix on at upgrade, within the existing spend caps. Turn it off in Govern › Self-fix.

**Two apps in one project**

- A new app built into a project that already has one starts from main and is never blocked by the other app's unmerged branch, including a Command Center template built Into the project. Only a person's choice or BOSS's own template launch can mark a build as a new app; an agent, a webhook or a run token cannot.

**Faster queues**

- A build gives the heavy-build slot back during design, spec, plan and research, and takes it back before any writing, sandbox command or test run.
- Default capacity is sized from the machine's cores and memory, and Setup says why.
- The system doctor now acts on a long queue: when work has waited 10 minutes and the machine has room, it adds a slot (listed in Decided for you, with Undo), and drains it when room runs out. A limit you set is never raised; if it is what holds the queue, the waiting build says so.

**Recovery our own fault simulation found**

- A repair that changed nothing runnable re-checks that the app starts, so a crashing app can no longer finish green.
- An app that ignores the port BOSS gives it fails with "listening on N, expected $PORT".
- A key only a person has (an external provider's API key named by the app's start error or marked required in .env.example) raises one card that names the app and why; BOSS mints signing keys itself and never asks for BOSS, GitHub, cloud or deploy-platform credentials.
- The same cause recognised across attempts: three tries of one cause, then a person decides. A green attempt or a person's Retry starts a fresh count.
- Every build, repair and follow-up is told which app it is in, and a check fails a change that edits another deployable app.

**Memory**

- The API no longer grows on a long-running install. List screens and background checks stopped loading every build's saved files (the console's build list fell from about 24 MB to about 1 MB per poll), and the API caps and returns freed allocator memory. A pre-release check (`scripts/memory-budget.sh`) keeps it bounded: about 350 MB steady in our run.

## Known issues

- A person's scoped CLI key cannot resolve a review comment; it needs a console session (decision pending).

## Checks

- Every PR had an independent review, repeated until a review found nothing serious. Reviews caught, among others: a clean-up that could clear other projects' build cache, a self-fix path that could run host commands, and an agent path past the new-app protection.
- Each PR passed the local merge gate on the exact commit merged, and the plumbing and engine end-to-end gates.
- **Laptop install upgraded to this commit:** plumbing 20 of 20, engines 21 of 21, golden journeys 2 of 2. The plumbing gate needed a test-only fix (its stand-in for the app-start check did not yet pass the new argument from ENT-REC-09); that fix is part of this tag.
- **Memory:** right after the upgrade the API process held about 480 MB. The pre-release memory check settled at about 354 MB with 4 MB of growth over 5 minutes.
- **Not re-run for this candidate:** the locked-down enterprise lane (rc10.37 passed it).

Thanks to an enterprise contributor for the validation run and the fixes.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
