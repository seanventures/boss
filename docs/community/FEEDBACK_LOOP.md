# How a report becomes a change

Report a bug, a setup problem, or a feature request, and follow it from the first report to a shipped fix. GitHub issues are the public record for every report.

To file one, [open a new issue](https://github.com/seanventures/boss/issues/new/choose) and pick the bug, installation, or feature form. You only need to describe what you were doing, what you expected, and what happened.

!!! warning "Security vulnerabilities"
    Do not open a public issue for a suspected vulnerability. Email **boss@sean.net** instead, and leave credentials out of the message. [SECURITY.md](https://github.com/seanventures/boss/blob/main/SECURITY.md) explains what to include.

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
