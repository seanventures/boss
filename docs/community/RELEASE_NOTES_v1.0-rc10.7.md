# BOSS Community 1.0.0-rc.10.7 candidate

RC10.7 builds on rc10.6. Everything in it came from a second full HelpDesk AI run, recorded end to end:
- the app built from a brief and deployed;
- a second feature (ticket tags) added onto the live app;
- a third (a new look with a dashboard and dark mode) built and deployed on top.

Each failure on that run was fixed in BOSS as a class, with tests. This is a test candidate.

## What changed

### Agents recover first

- **A failed step goes to its own agent before a person.** The step's recovery agent reads the failure and the machine, then acts within a budget: retry, fix the app, clean up and retry, wait for a BOSS update, or ask a person one question. It never repeats an action that already failed. A cause in BOSS itself is recorded as a recommendation for BOSS's developers, and the step resumes by itself once BOSS is updated.
- **Open review findings go to the agent too.** A build stopped on findings it could not fix is handed to its recovery agent with the findings word for word, before anyone is asked to waive a check.
- **A step follows the Retry a person pressed.** It no longer keeps waiting on the attempt that was replaced.

### A pull request opens even when checks fail

- **New project setting, "Open the PR when checks fail" (on by default).** After its repair rounds, a build whose checks still fail opens its pull request instead of stopping for a waiver:
  - the PR opens as a draft with the label `checks failing`;
  - its body lists every failing check and finding, security first;
  - a follow-up task is started to fix them;
  - the run carries on to the merge decision.
- **The merge card leads with the failing checks.** A plain Approve is refused. "Merge with checks failing" needs a written reason and is recorded.
- **"Merged" means merged.** A pull request is shown as merged only when it really was, either from GitHub's state or from a recorded merge commit. An approval with no merge behind it reads "Approved · not merged". A deploy of an unmerged PR says so.

### Deploys

- **A new release retires the app's earlier release** from the database's shared project before it starts, so the old container no longer holds the port. Only containers started from the app's own folder are removed, never the database or another app's.
- **A release that brings its own database is not handed the old one.** The address BOSS's database was bound to for an earlier release no longer reaches a release that runs its own.
- **Roll back works for a release deployed from a declared manifest.**
- **Roll back and Redeploy never replay a removal.** Before this, a Roll back could re-run an earlier governed removal, including deleting its data, without a new approval. A removal is now only ever its own act, with the typed app name and its own approval.
- **Remove app stops the containers that are actually running,** including the shared project. In Deploys a removal reads **Removed**, never Live, and **Remove app…** is reachable from any deploy's menu.

### Builds

- **Every new change to an app plans itself.** Before this, a follow-up build reused the previous build's plan: a "new look" build re-implemented the previous feature. The earlier plan is now reference only.
- **BOSS's own description of the deploy machine is no longer read as the brief asking for Java, Ruby or PHP.**
- **A next attempt is never merged into the attempt it replaces.**

### Console

- **A single-page app previews at its own paths.** A React Router app no longer renders blank under the preview address, and a reload comes back to the preview.
- **App Setup:**
  - the "Set up sign-in" chip sits under the app name instead of hiding it;
  - the BOSS sign-in row gives a reason and a Try again button instead of "Failed to fetch";
  - an approval-seat refusal tells apart two people with the same name.
- **The full console test suite passes:** 7,367 tests.

## Validation

- **Release gate, plumbing:** 19 of 19 checks in 57 s.
- **Release gate, real agent:** a full build (Onboarding Path Builder) finished in 12.3 min against a 25 min budget and opened its PR.
- **Full suites:** engine, API and console (results in the release description).
- **The HelpDesk AI run on this branch:** brief, PRD, architecture, build, PR, deploy, monitoring, then two features added onto the live app and deployed.

## Known issues

- **A manual Deploy from the Infra drawer** of an SSH target can take the old single-container path and fail at transfer. Workflow deploys are not affected.
- **On a Community install the release gate's test-account step needs the seed module copied in.** Its printed hint names a command that the install's image does not include. This will be fixed.
- **Recovery agents diagnose BOSS-side causes but cannot yet change BOSS's code.** That is the headline of the next candidate, "BOSS fixes itself".

## Install and upgrade

Select `v1.0-rc10.7` explicitly with the installer's `--ref` option, from a checkout of that same tag. See the [Quickstart](QUICKSTART.md).

Before upgrading an existing install, take a complete protected backup (`boss upgrade` does this). Changing the checkout back does not undo database migrations.
