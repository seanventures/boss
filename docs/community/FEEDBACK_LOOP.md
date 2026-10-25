# How a report becomes a change

Report a bug, a setup problem, or a feature request, and follow it from the first report to a shipped fix. GitHub issues are the public record for every report.

To file one, [open a new issue](https://github.com/seanventures/boss/issues/new/choose) and pick the bug, installation, or feature form. You only need to describe what you were doing, what you expected, and what happened.

!!! warning "Security vulnerabilities"
    Do not open a public issue for a suspected vulnerability. Email **boss@sean.net** instead, and leave credentials out of the message. [SECURITY.md](https://github.com/seanventures/boss/blob/main/SECURITY.md) explains what to include.

## Stuck during your first run?

1. In the install directory, run `boss status` and `boss doctor`. A healthy API proves the service is reachable; it does not prove your model, repository or build works.
2. If sign-in cannot reach BOSS, keep the install running, check your SSH tunnel or HTTPS proxy, and use **Retry**. Follow [Sign in](CONSOLE_AUTH.md) for claim or account recovery.
3. If a model or repository is blocked, open **Repo and models** and read that connection's status. A model **Test** makes a small governed call; reviewing the status does not.
4. If work stops, open the run in **Workflow** and read the failed or waiting step. Resolve its named cause or approval before retrying. Keep the run ID and receipt or request ID, if shown.
5. [Report the problem](https://github.com/seanventures/boss/issues/new/choose) with the installed release (`cat .boss-release`), OS, install method, what you tried, the exact error and the run ID. Copy only relevant lines from `boss doctor`; remove private paths, repository content and account information.

Never attach `.env`, `boss-community.env.sh`, a setup link, tokens, authentication files, browser cookies, private logs or a backup archive. Backups include identity keys and private data. If you suspect a vulnerability, use the private security contact above.

## Download a report for one run (RC9)

RC9 source adds **Download diagnostics** in a Factory build's **More** menu and beside the sharing control for a workflow run. The published RC8 release predates this local download.

Open the report, inspect it and edit out anything private. BOSS uses its existing authenticated, project-scoped run-report service: the report contains bounded errors, step records and log excerpts, after the server's secret, personal-data and installation-name redaction. Redaction cannot recognize every private detail; the preview is yours to check. Editing the report resets its inspection checkbox.

Check **I checked the report for private content**, then choose **Download report**. BOSS rechecks access to that run and saves the edited snapshot as `boss-run-diagnostics.txt`. A missing run or denied permission prevents the download. Local download works with telemetry off or no collector configured; it makes no model call and sends nothing to the BOSS project. The separate **Send** action in **Share this run** retains its own permission and sharing controls.

Review the saved file again before attaching it to a report. Add the installed release or exact commit (`cat .boss-release`), OS, reproduction steps and the run ID yourself. This report is a bounded snapshot, not a full log archive, provider-invoice reconciliation or proof that an external effect stopped. For an uncertain provider outcome, preserve the original IDs and reconcile it before retrying.

## From report to release

1. **You report it.** Include the version or commit, the install method, and the edition, and remove credentials and private data. The issue stays open while it is investigated.
2. **A maintainer reproduces it.** Maintainers try the steps, ask for any missing details, or link a duplicate. Bug and installation reports wait for a person. A complete feature request may receive a draft plan, but that plan cannot start work, spend money, or change a repository.
3. **A maintainer decides.** The issue gets one of these outcomes: accepted, with the scope; needs more information; duplicate; deferred; or declined, with a reason.
4. **An accepted change becomes a pull request.** The pull request names its tests and any budget, receipt, or database change. Bots may comment, but only people merge.
5. **The issue closes when the fix is linked.** Before closing, a maintainer links the change that shipped and explains how to check it. If the result doesn't match what you reported, say so on the issue or file a regression.

## The optional GitHub App

Maintainers can enable a signed GitHub App. With it, BOSS posts one acknowledgement on a new issue and records a decision that waits for a person. The acknowledgement is not a fix or a commitment to ship. Enabling the App requires configuring a signed webhook and reviewing its permissions first. Without it, people carry out the same steps by hand.

## What a report does not do

- Opening an issue does not authorize any of the steps above, and does not spend money, run code, write to a provider, deploy, or merge.
- An acknowledgement is not a resolution, and a draft plan is not permission to start work.
- This process collects no usage data from your self-hosted install. The only information shared is what you choose to put in the issue.

## Next steps

- [Contributing](https://github.com/seanventures/boss/blob/main/CONTRIBUTING.md), if you want to propose the change yourself.
- [Quickstart](QUICKSTART.md), if you are still getting the console running.
- [Guide index](README.md), for the rest of the docs.
